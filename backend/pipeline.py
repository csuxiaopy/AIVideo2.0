from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from backend import models
from backend.capabilities import CORE_CAPABILITIES, mode_is_active
from backend.config import Settings
from backend.database import utc_now
from backend.detectors.black_screen import is_black_screen
from backend.detectors.fire_smoke import FireSmokeDetector
from backend.detectors.yolo import YoloDetector
from backend.eventbus import EventBus
from backend.media import MediaGateway, PersonOverlay, SafetyOverlay, draw_person_overlays, draw_safety_overlays
from backend.queueing import AnalysisQueue
from backend.repository import Repository, as_json, from_json
from backend.rules import RuleStateRegistry, is_scheduled, point_in_polygon
from backend.schemas import CameraOptions, GeometrySpec, Mode, ScheduleSpec
from backend.security import SecretCipher
from backend.vlm import VLMError, VisionModelClient
from backend.webhook import WebhookClient


logger = logging.getLogger(__name__)
TASKS = Counter("monitor_tasks_total", "Processed tasks", ["status"])
ANALYSIS_LATENCY = Histogram("monitor_analysis_seconds", "Camera task latency")
ONLINE_GAUGE = Gauge("monitor_cameras_online", "Online cameras")
QUEUE_GAUGE = Gauge("monitor_queue_depth", "Queue depth", ["priority"])
VLM_CALLS = Counter("monitor_vlm_calls_total", "VLM calls", ["mode", "status"])


class MonitoringRuntime:
    def __init__(self, settings: Settings, repository: Repository, cipher: SecretCipher):
        self.settings = settings
        self.repository = repository
        self.cipher = cipher
        self.event_bus = EventBus()
        self.queue = AnalysisQueue(settings.redis_url)
        self.fire_queue = AnalysisQueue(settings.redis_url, prefix="monitor:fire-tasks")
        self.rules = RuleStateRegistry()
        self.media = MediaGateway(self.repository.set_camera_runtime)
        detector_settings = self.repository.get_detector_settings()
        self.yolo = YoloDetector(
            detector_settings.general_model or settings.yolo_model,
            detector_settings.general_device or settings.yolo_device,
            settings.yolo_imgsz,
            settings.yolo_confidence,
        )
        self.fire_smoke = FireSmokeDetector(
            detector_settings.fire_smoke_model or settings.fire_smoke_model,
            detector_settings.fire_smoke_device or settings.fire_smoke_device,
            settings.fire_smoke_imgsz,
            detector_settings.model_sha256 or settings.fire_smoke_sha256,
        )
        self.webhook = WebhookClient()
        self.vlm: VisionModelClient | None = None
        self.scheduler_task: asyncio.Task | None = None
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
        await self.sync_cameras()
        self.running = True
        if self.settings.scheduler_enabled:
            self.scheduler_task = asyncio.create_task(self._scheduler(), name="camera-scheduler")
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
        tasks = [task for task in [self.scheduler_task, *self.worker_tasks, *self.fire_worker_tasks] if task]
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
            detector_settings.general_model,
            detector_settings.general_device,
            self.settings.yolo_imgsz,
            self.settings.yolo_confidence,
        )
        self.fire_smoke = FireSmokeDetector(
            detector_settings.fire_smoke_model,
            detector_settings.fire_smoke_device,
            self.settings.fire_smoke_imgsz,
            detector_settings.model_sha256 or self.settings.fire_smoke_sha256,
        )

    async def sync_cameras(self) -> None:
        cameras = self.repository.list_cameras()
        media_specs = [
            (camera.id, self.cipher.decrypt(camera.rtsp_url_encrypted), camera.enabled) for camera in cameras
        ]
        await self.media.sync(media_specs)
        now = time.monotonic()
        for index, camera in enumerate(cameras):
            self.next_run.setdefault(camera.id, now + index / max(1, len(cameras)))
            self.next_fire_run.setdefault(camera.id, now + index / max(1, len(cameras)))
        ONLINE_GAUGE.set(sum(1 for camera in cameras if camera.online))

    async def analyze_now(self, camera_id: str) -> dict[str, Any]:
        camera = self.repository.get_camera(camera_id)
        if not camera:
            raise KeyError(camera_id)
        async with self.camera_locks[camera_id]:
            general = await self._process(camera, force=True)
            modes = set(from_json(camera.modes_json, []))
            if Mode.FIRE_SMOKE.value in modes:
                fire = await self._process_fire(camera, force=True)
                general["results"].extend(fire["results"])
            return general

    async def _scheduler(self) -> None:
        while self.running:
            now = time.monotonic()
            cameras = self.repository.list_cameras()
            for camera in cameras:
                if not camera.enabled:
                    continue
                options = CameraOptions.model_validate(from_json(camera.options_json, {}))
                modes = set(from_json(camera.modes_json, []))
                general_modes = modes - {Mode.FIRE_SMOKE.value}
                period = min(options.health_interval_seconds, max(1.0, 1.0 / options.yolo_fps))
                general_due = general_modes and now >= self.next_run.get(camera.id, now)
                if general_due:
                    self.next_run[camera.id] = now + period
                    if camera.id not in self.queued:
                        priority = "high" if general_modes & {Mode.BLACK_SCREEN.value, Mode.INTRUSION.value} else "low" if general_modes == {Mode.PEOPLE_FLOW.value} else "normal"
                        task_id = await self.queue.enqueue(camera.id, priority)
                        if task_id:
                            self.queued.add(camera.id)
                fire_period = max(0.2, 1.0 / options.fire_smoke_fps)
                if Mode.FIRE_SMOKE.value in modes and now >= self.next_fire_run.get(camera.id, now):
                    self.next_fire_run[camera.id] = now + fire_period
                    if camera.id not in self.fire_queued:
                        task_id = await self.fire_queue.enqueue(camera.id, "critical")
                        if task_id:
                            self.fire_queued.add(camera.id)
            for priority, depth in (await self.queue.depths()).items():
                QUEUE_GAUGE.labels(priority=priority).set(depth)
            for priority, depth in (await self.fire_queue.depths()).items():
                QUEUE_GAUGE.labels(priority=f"fire_{priority}").set(depth)
            self.last_heartbeat = utc_now()
            await asyncio.sleep(0.5)

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
                        else:
                            await self._process(camera)
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

    async def _process(self, camera: models.Camera, force: bool = False) -> dict[str, Any]:
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
                await self._alert(camera, analysis, frame.jpeg)

        yolo_modes = modes - {Mode.BLACK_SCREEN.value}
        detections = []
        if yolo_modes:
            try:
                detections = await asyncio.to_thread(self.yolo.detect, camera.id, frame.jpeg)
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
        post_people = [item for item in people if item.confidence >= 0.45]
        foot_points = [((item.box[0] + item.box[2]) / 2, item.box[3]) for item in post_people]
        occupied = any(point_in_polygon(point, geometry.post_roi) for point in foot_points)

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
                    await self._alert(camera, analysis, frame.jpeg)

        if Mode.PEOPLE_FLOW.value in modes and geometry.flow_line and mode_is_active(Mode.PEOPLE_FLOW.value, schedule, now):
            tracks = [
                (item.track_id, ((item.box[0] + item.box[2]) / 2, item.box[3]))
                for item in people if item.track_id is not None
            ]
            entered, exited = state.flow_update(tracks, geometry.flow_line, now)
            self.repository.upsert_traffic(camera.id, len(people), entered, exited)
            results.append({"mode": Mode.PEOPLE_FLOW.value, "current": len(people), "entered": entered, "exited": exited})

        if Mode.PHONE_USE.value in modes and mode_is_active(Mode.PHONE_USE.value, schedule, now) and self._mode_due(camera.id, Mode.PHONE_USE.value, options.behavior_interval_seconds, force):
            roi_people = [item for item in people if point_in_polygon(((item.box[0] + item.box[2]) / 2, item.box[3]), geometry.post_roi)]
            roi_ids = {id(item) for item in roi_people}
            phone_candidates = roi_people + [item for item in detections if item.class_name in {"cell phone", "mobile phone"}]
            if roi_ids and self.yolo.phone_candidate(phone_candidates):
                result = await self._behavior(camera, Mode.PHONE_USE, state, options, frame.jpeg)
                results.append(result)
            else:
                self.repository.add_analysis(
                    camera_id=camera.id, mode=Mode.PHONE_USE.value, status="none", confidence=0.9,
                    reason="YOLO 未发现人员附近的手机候选", latency_ms=0,
                )
                results.append({"mode": Mode.PHONE_USE.value, "status": "none"})

        if Mode.SMOKING.value in modes and people and mode_is_active(Mode.SMOKING.value, schedule, now) and self._mode_due(camera.id, Mode.SMOKING.value, options.behavior_interval_seconds, force):
            result = await self._behavior(camera, Mode.SMOKING, state, options, frame.jpeg)
            results.append(result)

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
                people_overlay = [PersonOverlay(item.box, item.confidence, item.track_id) for item in people]
                evidence = draw_person_overlays(frame.jpeg, people_overlay)
                evidence = draw_safety_overlays(
                    evidence,
                    [],
                    geometry.intrusion_zone.points,
                    people_overlay,
                    set(triggered_ids),
                )
                await self._alert(camera, analysis, evidence, bypass_cooldown=True)
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
                overlays = [SafetyOverlay(item.class_name, item.box, item.confidence) for item in qualified]
                evidence = draw_safety_overlays(frame.jpeg, overlays)
                await self._alert(camera, analysis, evidence)
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

    async def _behavior(
        self, camera: models.Camera, mode: Mode, state, options: CameraOptions, fallback_jpeg: bytes
    ) -> dict[str, Any]:
        frames = [packet.jpeg for packet in self.media.sample(camera.id, 8)] or [fallback_jpeg]
        if not self.vlm:
            analysis = self.repository.add_analysis(
                camera_id=camera.id, mode=mode.value, status="uncertain", confidence=0,
                severity="normal",
                reason="视觉大模型尚未配置", error="model_not_configured", latency_ms=0,
            )
            return {"mode": mode.value, "status": "uncertain", "reason": analysis.reason}
        try:
            response = await self.vlm.tiered_analyze(mode, frames)
            VLM_CALLS.labels(mode=mode.value, status=response.result.status).inc()
            analysis = self.repository.add_analysis(
                camera_id=camera.id, mode=mode.value, status=response.result.status,
                confidence=response.result.confidence, reason=response.result.reason,
                severity="normal",
                request_id=response.request_id, provider=response.provider, model=response.model,
                usage_json=as_json(response.usage), latency_ms=response.latency_ms,
            )
            confirmed = state.behavior_confirmed(mode.value, response.result.status == "confirmed", utc_now())
            if confirmed:
                await self._alert(camera, analysis, fallback_jpeg)
            return {
                "mode": mode.value, "status": response.result.status,
                "confirmed_window": confirmed, "reason": response.result.reason,
            }
        except VLMError as exc:
            VLM_CALLS.labels(mode=mode.value, status="error").inc()
            self.repository.add_analysis(
                camera_id=camera.id, mode=mode.value, status="uncertain", confidence=0,
                severity="normal",
                reason="视觉大模型分析失败", request_id=exc.request_id,
                error=f"{type(exc).__name__}: {str(exc)[:500]}", latency_ms=0,
            )
            return {"mode": mode.value, "status": "uncertain", "error": str(exc)}

    async def _alert(
        self, camera: models.Camera, analysis: models.Analysis, jpeg: bytes, bypass_cooldown: bool = False
    ) -> None:
        options = CameraOptions.model_validate(from_json(camera.options_json, {}))
        cooldown_seconds = options.alert_cooldown_seconds
        if analysis.mode == Mode.FIRE_SMOKE.value:
            cooldown_seconds = 60
        elif analysis.mode == Mode.INTRUSION.value:
            cooldown_seconds = options.intrusion_cooldown_seconds
        last = self.repository.latest_alert_time(camera.id, analysis.mode)
        if not bypass_cooldown and last and utc_now() - last < timedelta(seconds=cooldown_seconds):
            return
        filename = f"{camera.id}-{analysis.mode}-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}.jpg"
        path = self.settings.evidence_dir / filename
        path.write_bytes(jpeg)
        alert = self.repository.add_alert(
            camera_id=camera.id, analysis_id=analysis.id, mode=analysis.mode,
            status="confirmed", confidence=analysis.confidence, reason=analysis.reason,
            severity=analysis.severity, zone_name=analysis.zone_name,
            local_model=analysis.local_model, model_version=analysis.model_version,
            evidence_path=filename, webhook_status="shadow" if self.settings.shadow_mode else "pending",
            shadow=self.settings.shadow_mode,
        )
        payload = {
            "type": "alert", "id": alert.id, "camera_id": camera.id, "camera_name": camera.name,
            "mode": alert.mode, "status": alert.status, "confidence": alert.confidence,
            "severity": alert.severity, "scene_type": camera.scene_type,
            "zone_name": alert.zone_name,
            "fire_smoke_class": alert.zone_name if alert.mode == Mode.FIRE_SMOKE.value else None,
            "reason": alert.reason, "created_at": alert.created_at.isoformat(),
            "evidence_url": f"/evidence/{filename}", "shadow": alert.shadow,
        }
        await self.event_bus.publish(payload)
        if not self.settings.shadow_mode:
            webhook_settings = self.repository.get_webhook_settings()
            if webhook_settings.enabled and webhook_settings.url and webhook_settings.secret_encrypted:
                secret = self.cipher.decrypt(webhook_settings.secret_encrypted)
                asyncio.create_task(self._deliver_webhook(alert.id, webhook_settings.url, secret, payload))

    async def _deliver_webhook(self, alert_id: int, url: str, secret: str, payload: dict[str, Any]) -> None:
        try:
            await self.webhook.send(url, secret, payload)
            self.repository.update_alert_webhook(alert_id, "delivered")
        except Exception:
            self.repository.update_alert_webhook(alert_id, "failed")
            logger.exception("Webhook delivery failed for alert %s", alert_id)

    async def status(self) -> dict[str, Any]:
        general_depth = await self.queue.depths()
        fire_depth = await self.fire_queue.depths()
        return {
            "scheduler": {"status": "running" if self.running else "stopped", "last_heartbeat": self.last_heartbeat.isoformat()},
            "media": {"status": "running", "streams": len(self.media.streams)},
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
                "general": {"status": "ready" if self.yolo.available else "degraded", "detail": self.yolo.detail},
                "fire_smoke": self.fire_smoke.status(),
            },
            "shadow_mode": self.settings.shadow_mode,
        }
