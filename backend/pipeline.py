from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from backend import models
from backend.alerts import AlertService
from backend.annotation import annotate_detections
from backend.capabilities import CORE_CAPABILITIES, mode_is_active
from backend.cleanup import CleanupService
from backend.config import Settings
from backend.database import utc_now
from backend.detectors.black_screen import is_black_screen
from backend.detectors.fire_smoke import FireSmokeDetector
from backend.detectors.yolo import YoloDetector
from backend.eventbus import EventBus
from backend.media_capture import MediaGateway
from backend.queueing import AnalysisQueue
from backend.repository import Repository, as_json, from_json
from backend.rules import RuleStateRegistry, box_intersects_polygon, is_scheduled, point_in_polygon
from backend.schemas import CameraOptions, Detection, GeometrySpec, Mode, ScheduleSpec
from backend.security import SecretCipher
from backend.vlm import VLMError, VisionModelClient
from backend.webhook import WebhookClient


logger = logging.getLogger(__name__)
TASKS = Counter("monitor_tasks_total", "Processed tasks", ["status"])
ANALYSIS_LATENCY = Histogram("monitor_analysis_seconds", "Camera task latency")
ONLINE_GAUGE = Gauge("monitor_cameras_online", "Online cameras")
QUEUE_GAUGE = Gauge("monitor_queue_depth", "Queue depth", ["priority"])
VLM_CALLS = Counter("monitor_vlm_calls_total", "VLM calls", ["mode", "status"])
BEHAVIOR_INTERVAL_SECONDS = 180


def yolo_required_modes(modes: set[str]) -> set[str]:
    """Return modes that still depend on the general object detector."""
    return modes - {Mode.BLACK_SCREEN.value, Mode.PHONE_USE.value, Mode.SMOKING.value}


def staggered_capture_times(cameras: list[models.Camera], now: float) -> dict[str, float]:
    """Spread cameras with the same interval evenly across that complete interval."""
    groups: dict[int, list[models.Camera]] = defaultdict(list)
    for camera in cameras:
        if camera.enabled:
            groups[camera.frame_interval_seconds or 60].append(camera)
    schedule: dict[str, float] = {}
    for interval, group in groups.items():
        for index, camera in enumerate(group):
            schedule[camera.id] = now + index * interval / max(1, len(group))
    return schedule


class MonitoringRuntime:
    def __init__(self, settings: Settings, repository: Repository, cipher: SecretCipher):
        self.settings = settings
        self.repository = repository
        self.cipher = cipher
        self.event_bus = EventBus()
        self.queue = AnalysisQueue(settings.redis_url, maxsize=settings.analysis_queue_maxsize)
        self.fire_queue = AnalysisQueue(
            settings.redis_url, maxsize=settings.analysis_queue_maxsize, prefix="monitor:fire-tasks"
        )
        self.rules = RuleStateRegistry()
        self.media = MediaGateway(
            self.repository.set_camera_runtime,
            settings.snapshot_dir,
            settings.max_live_previews,
            settings.live_preview_fps,
            settings.live_preview_timeout_seconds,
            settings.frame_capture_timeout_seconds,
        )
        detector_settings = self.repository.get_detector_settings()
        self.yolo = YoloDetector(
            self._general_model_path(detector_settings.general_model),
            detector_settings.general_device or settings.yolo_device,
            settings.yolo_imgsz,
            settings.yolo_confidence,
            settings.yolo_iou,
        )
        self.fire_smoke = FireSmokeDetector(
            detector_settings.fire_smoke_model or settings.fire_smoke_model,
            detector_settings.fire_smoke_device or settings.fire_smoke_device,
            settings.fire_smoke_imgsz,
            detector_settings.model_sha256 or settings.fire_smoke_sha256,
        )
        self.webhook = WebhookClient()
        self.alerts = AlertService(settings, repository, cipher, self.event_bus, self.webhook)
        self.cleanup = CleanupService(settings, repository)
        self.vlm: VisionModelClient | None = None
        self.scheduler_task: asyncio.Task | None = None
        self.cleanup_task: asyncio.Task | None = None
        self.worker_tasks: list[asyncio.Task] = []
        self.fire_worker_tasks: list[asyncio.Task] = []
        self.running = False
        self.next_run: dict[str, float] = {}
        self.next_fire_run: dict[str, float] = {}
        self.last_mode_run: dict[tuple[str, str], float] = defaultdict(float)
        self.queued: set[str] = set()
        self.fire_queued: set[str] = set()
        self.camera_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.processed = 0
        self.failures = 0
        self.last_heartbeat = utc_now()
        self.last_detector_error: dict[str, float] = defaultdict(float)

    async def start(self) -> None:
        await self.queue.start()
        await self.fire_queue.start()
        await self.reload_models()
        await self.media.start()
        await self.sync_cameras()
        self.running = True
        if self.settings.scheduler_enabled:
            self.scheduler_task = asyncio.create_task(self._scheduler(), name="camera-scheduler")
            self.cleanup_task = asyncio.create_task(self._cleanup_loop(), name="alert-cleanup")
            self.worker_tasks = [
                asyncio.create_task(self._worker(index, fire_only=False), name=f"analysis-worker-{index}")
                for index in range(self.settings.analysis_workers)
            ]
            self.fire_worker_tasks = [
                asyncio.create_task(self._worker(index, fire_only=True), name=f"fire-worker-{index}")
                for index in range(self.settings.fire_smoke_workers)
            ]

    async def close(self) -> None:
        self.running = False
        tasks = [task for task in [self.scheduler_task, self.cleanup_task, *self.worker_tasks, *self.fire_worker_tasks] if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.media.close()
        await self.queue.close()
        await self.fire_queue.close()
        await self.webhook.close()
        if self.vlm:
            await self.vlm.close()

    async def reload_models(self) -> None:
        model_settings = self.repository.get_model_settings()
        if self.vlm:
            await self.vlm.close()
            self.vlm = None
        if model_settings.provider == "mock":
            return
        if model_settings.base_url and model_settings.api_key_encrypted:
            self.vlm = VisionModelClient(
                model_settings.base_url,
                self.cipher.decrypt(model_settings.api_key_encrypted),
                model_settings.economy_model,
                model_settings.enhanced_model,
            )

    async def reload_detectors(self) -> None:
        detector_settings = self.repository.get_detector_settings()
        self.yolo = YoloDetector(
            self._general_model_path(detector_settings.general_model),
            detector_settings.general_device,
            self.settings.yolo_imgsz,
            self.settings.yolo_confidence,
            self.settings.yolo_iou,
        )
        self.fire_smoke = FireSmokeDetector(
            detector_settings.fire_smoke_model,
            detector_settings.fire_smoke_device,
            self.settings.fire_smoke_imgsz,
            detector_settings.model_sha256 or self.settings.fire_smoke_sha256,
        )

    def _general_model_path(self, stored_model: str) -> str:
        """Keep custom weights safe; configuration owns known official model choices."""
        if stored_model and stored_model.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] not in {
            "yolo26n.pt", "yolo26s.pt", "yolo26m.pt"
        }:
            logger.warning("Preserving configured custom YOLO weight: %s", stored_model)
            return stored_model
        return self.settings.yolo_model_path

    async def sync_cameras(self) -> None:
        cameras = self.repository.list_cameras()
        media_specs = [
            (camera.id, self.cipher.decrypt(camera.rtsp_url_encrypted), camera.enabled) for camera in cameras
        ]
        await self.media.sync(media_specs)
        now = time.monotonic()
        enabled = [camera for camera in cameras if camera.enabled]
        # Recompute the complete plan after a camera is added, removed, enabled or edited.
        # Keeping old due times while bulk-adding cameras would cluster new cameras near
        # the end of the period instead of preserving an even distribution.
        self.next_run = staggered_capture_times(enabled, now)
        ONLINE_GAUGE.set(sum(1 for camera in cameras if camera.online))

    async def analyze_now(self, camera_id: str) -> dict[str, Any]:
        camera = self.repository.get_camera(camera_id)
        if not camera:
            raise KeyError(camera_id)
        async with self.camera_locks[camera_id]:
            recovering = not camera.online
            await self.media.capture(camera_id)
            general = await self._process(camera, force=True, recovering=recovering)
            modes = set(from_json(camera.modes_json, []))
            if Mode.FIRE_SMOKE.value in modes:
                fire = await self._process_fire(camera, force=True)
                general["results"].extend(fire["results"])
            self.repository.set_last_analysis_at(camera_id)
            return general

    async def _scheduler(self) -> None:
        while self.running:
            now = time.monotonic()
            cameras = self.repository.list_cameras()
            for camera in cameras:
                if not camera.enabled:
                    continue
                modes = set(from_json(camera.modes_json, []))
                period = camera.frame_interval_seconds or 60
                if modes and now >= self.next_run.get(camera.id, now):
                    self.next_run[camera.id] = now + period
                    if camera.id not in self.queued:
                        priority = (
                            "critical" if Mode.FIRE_SMOKE.value in modes
                            else "high" if modes & {Mode.BLACK_SCREEN.value, Mode.INTRUSION.value}
                            else "low" if modes == {Mode.PEOPLE_FLOW.value}
                            else "normal"
                        )
                        task_id = await self.queue.enqueue(camera.id, priority)
                        if task_id:
                            self.queued.add(camera.id)
            for priority, depth in (await self.queue.depths()).items():
                QUEUE_GAUGE.labels(priority=priority).set(depth)
            for priority, depth in (await self.fire_queue.depths()).items():
                QUEUE_GAUGE.labels(priority=f"fire_{priority}").set(depth)
            self.last_heartbeat = utc_now()
            await asyncio.sleep(0.5)

    async def _cleanup_loop(self) -> None:
        while self.running:
            await asyncio.sleep(6 * 3600)
            try:
                retention = self.repository.get_retention_settings()
                if not retention.auto_cleanup_enabled:
                    continue
                result = await asyncio.to_thread(self.cleanup.run)
                logger.info("Alert cleanup finished: %s", result)
            except Exception:
                logger.exception("Alert cleanup failed")

    async def _worker(self, index: int, fire_only: bool) -> None:
        queue = self.fire_queue if fire_only else self.queue
        queued = self.fire_queued if fire_only else self.queued
        while self.running:
            task = await queue.get()
            acquired = False
            try:
                acquired = await queue.acquire_camera(task.camera_id)
                if not acquired:
                    continue
                async with self.camera_locks[task.camera_id]:
                    camera = self.repository.get_camera(task.camera_id)
                    if camera and camera.enabled:
                        if fire_only:
                            await self._process_fire(camera)
                            self.repository.set_last_analysis_at(camera.id)
                        else:
                            # Scheduled capture is short lived: FFmpeg exits after one JPEG.
                            recovering = not camera.online
                            await self.media.capture(camera.id)
                            await self._process(camera, recovering=recovering)
                            self.repository.set_last_analysis_at(camera.id)
                            modes = set(from_json(camera.modes_json, []))
                            if Mode.FIRE_SMOKE.value in modes and camera.id not in self.fire_queued:
                                task_id = await self.fire_queue.enqueue(camera.id, "critical")
                                if task_id:
                                    self.fire_queued.add(camera.id)
                TASKS.labels(status="ok").inc()
                self.processed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                TASKS.labels(status="failed").inc()
                self.failures += 1
                logger.exception("Camera task failed: %s", task.camera_id)
            finally:
                if acquired:
                    await queue.release_camera(task.camera_id)
                queued.discard(task.camera_id)
                await queue.ack(task)

    async def _process(self, camera: models.Camera, force: bool = False, recovering: bool = False) -> dict[str, Any]:
        started = time.perf_counter()
        frame = self.media.latest(camera.id)
        if not frame:
            # A newly started stream normally needs a short FFmpeg warm-up.
            # Scheduled work should be skipped instead of reported as a worker
            # failure; an explicit user-requested analysis still returns an error.
            if not force:
                return {"camera_id": camera.id, "results": [], "skipped": "no_frame"}
            raise RuntimeError("视频源尚无可用画面")
        modes = set(from_json(camera.modes_json, [])) - {Mode.FIRE_SMOKE.value}
        options = CameraOptions.model_validate(from_json(camera.options_json, {}))
        geometry = GeometrySpec.model_validate(from_json(camera.geometry_json, {}))
        schedule = ScheduleSpec.model_validate(from_json(camera.schedule_json, {}))
        state = self.rules.for_camera(camera.id)
        results: list[dict[str, Any]] = []
        now = utc_now()

        if Mode.BLACK_SCREEN.value in modes and self._mode_due(camera.id, Mode.BLACK_SCREEN.value, options.health_interval_seconds, force):
            black, metrics = is_black_screen(
                frame.jpeg, options.black_mean_max, options.black_std_max, options.black_ratio_min
            )
            triggered = state.black_update(black)
            analysis = self.repository.add_analysis(
                camera_id=camera.id, mode=Mode.BLACK_SCREEN.value,
                status="confirmed" if triggered else "none", confidence=1.0 if triggered else 0.99,
                severity="high", local_model="statistical-black-screen", model_version="1",
                reason=f"画面指标 mean={metrics['mean']:.1f}, std={metrics['std']:.1f}, near_black={metrics['near_black_ratio']:.1%}",
                usage_json=as_json(metrics), latency_ms=0,
            )
            results.append({"mode": Mode.BLACK_SCREEN.value, "status": analysis.status, "reason": analysis.reason})
            if triggered:
                await self.alerts.create(camera, analysis, frame.jpeg)

        yolo_modes = yolo_required_modes(modes)
        detections = []
        if yolo_modes:
            try:
                detections = await asyncio.wait_for(
                    asyncio.to_thread(self.yolo.detect, camera.id, frame.jpeg),
                    timeout=self.settings.yolo_inference_timeout_seconds,
                )
            except Exception as exc:
                if force or time.monotonic() - self.last_detector_error[camera.id] > 60:
                    self.last_detector_error[camera.id] = time.monotonic()
                    self.repository.add_analysis(
                        camera_id=camera.id, mode="detector", status="uncertain", confidence=0,
                        severity="high", local_model=self.yolo.model_name, model_version=self.yolo.model_name,
                        reason="YOLO 检测失败", error=f"{type(exc).__name__}: {str(exc)[:500]}", latency_ms=0,
                    )
                if force:
                    raise
                return {"camera_id": camera.id, "results": results, "detector_error": str(exc)}

        people = self.yolo.people(detections)
        self.media.set_person_detections(camera.id, people)
        self.media.set_object_detections(camera.id, detections)
        post_people = [item for item in people if item.confidence >= 0.30]
        occupied = any(box_intersects_polygon(item.box, geometry.post_roi) for item in post_people)

        if Mode.ON_DUTY.value in modes and mode_is_active(Mode.ON_DUTY.value, schedule, now) and self._mode_due(camera.id, Mode.ON_DUTY.value, 15, force):
            analysis = self.repository.add_analysis(
                camera_id=camera.id, mode=Mode.ON_DUTY.value, status="confirmed" if occupied else "none",
                confidence=max((item.confidence for item in people), default=0.99),
                reason="岗位区域内检测到人员" if occupied else "岗位区域内未检测到人员", latency_ms=0,
            )
            results.append({"mode": Mode.ON_DUTY.value, "status": analysis.status, "reason": analysis.reason})

        if Mode.OFF_DUTY.value in modes:
            scheduled = mode_is_active(Mode.OFF_DUTY.value, schedule, now)
            triggered = state.absence_update(
                occupied, scheduled, options.off_duty_seconds, now, options.shift_grace_seconds
            )
            if self._mode_due(camera.id, Mode.OFF_DUTY.value, 15, force) or triggered:
                status = "confirmed" if triggered else "none"
                reason = "排班内岗位区域持续无人，达到离岗阈值" if triggered else (
                    "岗位有人或尚未达到离岗阈值" if scheduled else "当前不在排班时段"
                )
                analysis = self.repository.add_analysis(
                    camera_id=camera.id, mode=Mode.OFF_DUTY.value, status=status,
                    confidence=0.99, severity="normal", local_model=self.yolo.model_name,
                    model_version=self.yolo.model_name, reason=reason, latency_ms=0,
                )
                results.append({"mode": Mode.OFF_DUTY.value, "status": status, "reason": reason})
                if triggered:
                    evidence = annotate_detections(frame.jpeg, people, zone=geometry.post_roi)
                    await self.alerts.create(camera, analysis, evidence)

        if Mode.PEOPLE_FLOW.value in modes and mode_is_active(Mode.PEOPLE_FLOW.value, schedule, now):
            tracks = [
                (item.track_id, ((item.box[0] + item.box[2]) / 2, (item.box[1] + item.box[3]) / 2))
                for item in people if item.track_id is not None
            ]
            entered, flow_states = state.flow_update(
                tracks, now,
                options.flow_min_stable_frames,
                options.flow_entry_edge_ratio,
                options.flow_reassociation_seconds,
                options.flow_reassociation_distance,
                options.stream_recovery_grace_seconds,
                recovering,
            )
            current_count = len(tracks)
            self.repository.upsert_traffic(camera.id, current_count, entered, 0)
            if options.flow_debug:
                summary = self.repository.traffic_summary()
                camera_summary = next((item for item in summary["cameras"] if item["camera_id"] == camera.id), {})
                self.media.set_flow_debug(
                    camera.id, flow_states, current_count, int(camera_summary.get("entered_today", 0)), entered
                )
            results.append({"mode": Mode.PEOPLE_FLOW.value, "current": current_count, "entered": entered})

        behavior_modes = {
            mode for mode in (Mode.PHONE_USE, Mode.SMOKING)
            if mode.value in modes and mode_is_active(mode.value, schedule, now)
        }
        if behavior_modes and self._mode_due(
            camera.id, "behavior", BEHAVIOR_INTERVAL_SECONDS, force
        ):
            results.extend(await self._behaviors(camera, behavior_modes, frame.jpeg))

        if Mode.INTRUSION.value in modes and geometry.intrusion_zone:
            intrusion_people = [item for item in people if item.confidence >= options.intrusion_confidence and item.track_id is not None]
            intrusion_tracks = [
                (item.track_id, ((item.box[0] + item.box[2]) / 2, item.box[3])) for item in intrusion_people
            ]
            triggered_ids = state.intrusion_update(
                intrusion_tracks,
                geometry.intrusion_zone.points,
                now,
                options.intrusion_cooldown_seconds,
            )
            inside_ids = {
                track_id for track_id, point in intrusion_tracks
                if point_in_polygon(point, geometry.intrusion_zone.points)
            }
            self.media.set_intrusion(camera.id, geometry.intrusion_zone.points, inside_ids)
            if triggered_ids:
                matching = [item for item in intrusion_people if item.track_id in triggered_ids]
                confidence = max((item.confidence for item in matching), default=0.0)
                analysis = self.repository.add_analysis(
                    camera_id=camera.id,
                    mode=Mode.INTRUSION.value,
                    status="confirmed",
                    confidence=confidence,
                    severity="high",
                    zone_name=geometry.intrusion_zone.name,
                    local_model=self.yolo.model_name,
                    model_version=self.yolo.model_name,
                    reason=f"人员进入禁区：{geometry.intrusion_zone.name}",
                    latency_ms=0,
                )
                evidence = annotate_detections(
                    frame.jpeg,
                    intrusion_people,
                    zone=geometry.intrusion_zone.points,
                    highlighted_track_ids=set(triggered_ids),
                    show_track_ids=True,
                )
                await self.alerts.create(camera, analysis, evidence, bypass_cooldown=True)
                results.append({"mode": Mode.INTRUSION.value, "status": "confirmed", "track_ids": triggered_ids})
        else:
            self.media.set_intrusion(camera.id, [], set())

        ANALYSIS_LATENCY.observe(time.perf_counter() - started)
        return {"camera_id": camera.id, "results": results}

    async def _process_fire(self, camera: models.Camera, force: bool = False) -> dict[str, Any]:
        frame = self.media.latest(camera.id)
        if not frame:
            if force:
                raise RuntimeError("视频源尚无可用画面")
            return {"camera_id": camera.id, "results": [], "skipped": "no_frame"}
        modes = set(from_json(camera.modes_json, []))
        if Mode.FIRE_SMOKE.value not in modes:
            return {"camera_id": camera.id, "results": []}
        options = CameraOptions.model_validate(from_json(camera.options_json, {}))
        state = self.rules.for_camera(camera.id)
        results: list[dict[str, Any]] = []

        if not self.fire_smoke.available:
            error_key = f"fire:{camera.id}"
            if force or time.monotonic() - self.last_detector_error[error_key] > 60:
                self.last_detector_error[error_key] = time.monotonic()
                analysis = self.repository.add_analysis(
                    camera_id=camera.id,
                    mode=Mode.FIRE_SMOKE.value,
                    status="uncertain",
                    confidence=0,
                    severity="critical",
                    reason="烟火检测器不可用，当前不能形成安全结论",
                    local_model=self.fire_smoke.version,
                    model_version=self.fire_smoke.version,
                    error=self.fire_smoke.detail,
                    latency_ms=0,
                )
                await self.event_bus.publish(
                    {
                        "type": "detector_failure",
                        "camera_id": camera.id,
                        "camera_name": camera.name,
                        "mode": Mode.FIRE_SMOKE.value,
                        "severity": "critical",
                        "reason": analysis.reason,
                    }
                )
            return {"camera_id": camera.id, "results": [], "detector_error": self.fire_smoke.detail}

        try:
            detections = await asyncio.to_thread(self.fire_smoke.detect, frame.jpeg)
        except Exception as exc:
            self.repository.add_analysis(
                camera_id=camera.id,
                mode=Mode.FIRE_SMOKE.value,
                status="uncertain",
                confidence=0,
                severity="critical",
                reason="烟火检测执行失败",
                local_model=self.fire_smoke.version,
                model_version=self.fire_smoke.version,
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
                latency_ms=self.fire_smoke.last_latency_ms,
            )
            if force:
                raise
            return {"camera_id": camera.id, "results": [], "detector_error": str(exc)}

        qualified = [
            item for item in detections
            if (item.class_name == "fire" and item.confidence >= options.fire_confidence)
            or (item.class_name == "smoke" and item.confidence >= options.smoke_confidence)
        ]
        self.media.set_safety_detections(camera.id, qualified)
        fire_items = [item for item in qualified if item.class_name == "fire"]
        smoke_items = [item for item in qualified if item.class_name == "smoke"]
        fire_confirmed, smoke_confirmed = state.fire_smoke_update(bool(fire_items), bool(smoke_items))

        for kind, items, confirmed in (
            ("fire", fire_items, fire_confirmed),
            ("smoke", smoke_items, smoke_confirmed),
        ):
            if not items:
                continue
            confidence = max(item.confidence for item in items)
            status = "confirmed" if confirmed else "suspected"
            reason = (
                "连续2帧检测到火焰" if kind == "fire" and confirmed else
                "最近5帧中至少3帧检测到烟雾" if kind == "smoke" and confirmed else
                f"检测到{('火焰' if kind == 'fire' else '烟雾')}候选，等待时序确认"
            )
            analysis = self.repository.add_analysis(
                camera_id=camera.id,
                mode=Mode.FIRE_SMOKE.value,
                status=status,
                confidence=confidence,
                severity="critical",
                zone_name=kind,
                local_model=self.fire_smoke.version,
                model_version=self.fire_smoke.model_hash[:12] or self.fire_smoke.version,
                reason=reason,
                usage_json=as_json({"class": kind, "detections": len(items)}),
                latency_ms=self.fire_smoke.last_latency_ms,
            )
            if confirmed:
                evidence = annotate_detections(frame.jpeg, qualified)
                await self.alerts.create(camera, analysis, evidence)
            results.append({"mode": Mode.FIRE_SMOKE.value, "class": kind, "status": status, "confidence": confidence})

        if force and not qualified:
            results.append({"mode": Mode.FIRE_SMOKE.value, "status": "none"})
        return {"camera_id": camera.id, "results": results}

    def _mode_due(self, camera_id: str, mode: str, interval: float, force: bool) -> bool:
        if force:
            self.last_mode_run[(camera_id, mode)] = time.monotonic()
            return True
        now = time.monotonic()
        key = (camera_id, mode)
        if now - self.last_mode_run[key] < interval:
            return False
        self.last_mode_run[key] = now
        return True

    async def _behaviors(
        self,
        camera: models.Camera,
        modes: set[Mode],
        frame_jpeg: bytes,
    ) -> list[dict[str, Any]]:
        if not self.vlm:
            output = []
            for mode in sorted(modes, key=lambda item: item.value):
                analysis = self.repository.add_analysis(
                    camera_id=camera.id, mode=mode.value, status="uncertain", confidence=0,
                    severity="normal", reason="视觉大模型尚未配置",
                    error="model_not_configured", latency_ms=0,
                )
                output.append({"mode": mode.value, "status": "uncertain", "reason": analysis.reason})
            return output
        try:
            response = await self.vlm.tiered_analyze_behaviors(modes, frame_jpeg)
            VLM_CALLS.labels(mode="behavior_combined", status="completed").inc()
            output = []
            for mode in sorted(modes, key=lambda item: item.value):
                result = response.results[mode]
                VLM_CALLS.labels(mode=mode.value, status=result.status).inc()
                analysis = self.repository.add_analysis(
                    camera_id=camera.id, mode=mode.value, status=result.status,
                    confidence=result.confidence, reason=result.reason, severity="normal",
                    request_id=response.request_id, provider=response.provider, model=response.model,
                    usage_json=as_json(response.usage), latency_ms=response.latency_ms,
                )
                confirmed = result.status == "confirmed"
                if confirmed:
                    await self.alerts.create(camera, analysis, frame_jpeg)
                output.append({
                    "mode": mode.value, "status": result.status,
                    "confirmed_window": confirmed, "reason": result.reason,
                })
            return output
        except VLMError as exc:
            VLM_CALLS.labels(mode="behavior_combined", status="error").inc()
            output = []
            for mode in sorted(modes, key=lambda item: item.value):
                VLM_CALLS.labels(mode=mode.value, status="error").inc()
                self.repository.add_analysis(
                    camera_id=camera.id, mode=mode.value, status="uncertain", confidence=0,
                    severity="normal", reason="视觉大模型分析失败", request_id=exc.request_id,
                    error=f"{type(exc).__name__}: {str(exc)[:500]}", latency_ms=0,
                )
                output.append({"mode": mode.value, "status": "uncertain", "error": str(exc)})
            return output

    async def status(self) -> dict[str, Any]:
        general_depth = await self.queue.depths()
        fire_depth = await self.fire_queue.depths()
        return {
            "scheduler": {"status": "running" if self.running else "stopped", "last_heartbeat": self.last_heartbeat.isoformat()},
            "media": {
                "status": "running",
                "registered_cameras": len(self.media.sources),
                "snapshots": len(self.media.snapshots),
                "active_previews": len(self.media.previews),
                "max_live_previews": self.media.max_live_previews,
            },
            "yolo": {"status": "ready" if self.yolo.available else "degraded", "detail": self.yolo.detail},
            "queue": {
                "status": "redis" if self.queue.redis_available else "in_memory",
                "depth": general_depth,
                "fire_depth": fire_depth,
            },
            "queues": {
                "general": sum(general_depth.values()),
                "fire": sum(fire_depth.values()),
                "general_by_priority": general_depth,
                "fire_by_priority": fire_depth,
            },
            "workers": {
                "count": len(self.worker_tasks) + len(self.fire_worker_tasks),
                "general_count": len(self.worker_tasks),
                "fire_count": len(self.fire_worker_tasks),
                "processed": self.processed,
                "failures": self.failures,
            },
            "detectors": {
                "general": self.yolo.status(),
                "fire_smoke": self.fire_smoke.status(),
            },
        }
