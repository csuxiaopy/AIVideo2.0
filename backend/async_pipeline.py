"""Production post-processing: loop-owned state, durable I/O and bounded reviews.

Only MonitoringRuntime uses the facade. API repositories retain their normal
commit semantics. A returned analysis DTO means submitted, NOT saved.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select

from backend import models
from backend.background import CriticalIngress, Job, ReviewQueue, ReviewRequest, atomic_evidence
from backend.database import session_scope, utc_now
from backend.repository import from_json
from backend.schemas import CameraOptions, Mode, ScheduleSpec
from backend.rules import is_scheduled
from backend.task_store import TaskStore

logger = logging.getLogger(__name__)
REVIEW: ContextVar[ReviewRequest | None] = ContextVar("monitor_review", default=None)


def json_values(values):
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in values.items()}


def db_values(values):
    return {key: datetime.fromisoformat(value) if key.endswith("_at") and isinstance(value, str) else value
            for key, value in values.items()}


def fingerprint(camera):
    names = ("id", "name", "enabled", "directory_id", "modes_json", "geometry_json", "schedule_json",
             "intrusion_schedule_json", "options_json", "rtsp_url_encrypted", "substream_url_encrypted")
    return hashlib.sha256(json.dumps({key: getattr(camera, key, None) for key in names}, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class CameraSnapshot:
    id: str
    name: str
    enabled: bool
    directory_id: int | None
    modes_json: str
    geometry_json: str
    schedule_json: str
    intrusion_schedule_json: str
    options_json: str
    rtsp_url_encrypted: str
    substream_url_encrypted: str | None
    frame_interval_seconds: int
    online: bool


class DeferredRepository:
    def __init__(self, owner):
        self.owner = owner

    def __getattr__(self, name):
        # Configuration load/admin operations are not realtime writes.
        return getattr(self.owner.original, name)

    def list_cameras(self):
        return [self.get_camera(key) for key in self.owner.cameras]

    def get_camera(self, camera_id):
        camera = self.owner.cameras.get(camera_id)
        if camera is None:
            return None
        # Frozen detached DTO, online derived from live memory rather than stale DB.
        from dataclasses import replace
        return replace(camera, online=self.owner.online(camera_id))

    def get_camera_directory(self, directory_id):
        value = self.owner.directories.get(directory_id)
        return SimpleNamespace(**value) if value else None

    def latest_directory_alert_time(self, directory_id, mode):
        # Authoritative cooldown is checked again in the alert transaction.
        return self.owner.cooldowns.get((directory_id, mode))

    def add_analysis(self, **values):
        camera = self.get_camera(values.get("camera_id"))
        defaults = dict(camera_name=camera.name if camera else "", confidence=0, reason="",
                        severity="normal", zone_name=None, local_model=None, model_version=None,
                        usage_json="{}", latency_ms=0, created_at=utc_now())
        defaults.update(values)
        job = Job.make("analysis_write", f"analysis:{uuid4()}", json_values(defaults), values.get("camera_id"))
        self.owner.submit(job)
        return SimpleNamespace(id=job.id, **defaults)

    def upsert_traffic(self, camera_id, current_count, entered, exited):
        bucket = utc_now().replace(second=0, microsecond=0).isoformat()
        key = camera_id, bucket
        item = self.owner.flow_pending.setdefault(key, {"camera_id": camera_id,
            "current_count": 0, "entered": 0, "exited": 0, "bucket_start": bucket})
        item["current_count"] = current_count
        item["entered"] += entered
        item["exited"] += exited
        self.owner.flow_counts[camera_id] = self.owner.flow_counts.get(camera_id, 0) + entered

    def traffic_summary(self):
        return {"cameras": [{"camera_id": key, "entered_today": value}
                            for key, value in self.owner.flow_counts.items()]}

    def add_model_call_log(self, **values):
        self.owner.submit(Job.make("model_log_write", f"model-log:{uuid4()}", json_values(values), values.get("camera_id")))


class AsyncPipeline:
    WRITE_KINDS = ["analysis_write", "traffic_write", "model_log_write", "alert_write"]

    def __init__(self, runtime, original, io):
        self.runtime, self.original, self.io = runtime, original, io
        runtime.webhook.io_pool = io
        self.store = TaskStore()
        self.ingress = CriticalIngress(io, self.store.persist, runtime.settings.background_queue_capacity)
        self.ingress.persist_batch = lambda jobs: self.store.persist_many(jobs)
        self.repository = DeferredRepository(self)
        self.cameras: dict[str, CameraSnapshot] = {}
        self.directories = {}
        self.version = 0
        self.signature = None
        self.cooldowns = {}
        self.alert_cooldowns = {}
        self.flow_counts = {}
        self.flow_pending = {}
        self.retained: dict[str, Job] = {}
        self.reserved_alerts = set()
        self.review_evidence: dict[str, bytes] = {}
        self.review_last = {}
        self.review_applied = {}
        self.stale_results = 0
        self.failed_cameras = set()
        self.global_write_failure = False
        self.durable_status = {}
        self.worker_errors = {}
        self.tasks = []
        self.reviews = ReviewQueue(self._review, self._review_completed,
                                   concurrency=runtime.settings.review_workers,
                                   max_keys=8192, timeout=runtime.settings.review_timeout_seconds)

    async def refresh(self):
        def load():
            cameras = [CameraSnapshot(**{key: getattr(row, key) for key in CameraSnapshot.__dataclass_fields__})
                       for row in self.original.list_cameras()]
            directories = self.original.list_camera_directories()
            return cameras, directories
        cameras, directories = await self.io.run(load)
        signature = tuple(sorted((c.id, fingerprint(c)) for c in cameras)), json.dumps(directories, sort_keys=True, default=str)
        if self.signature != signature:
            self.version += 1
            self.signature = signature
        self.cameras = {c.id: c for c in cameras}
        self.directories = {d["id"]: d for d in directories}

    def online(self, camera_id):
        stream = self.runtime.media.capture_streams.get(camera_id)
        frame = self.runtime.media.latest(camera_id)
        return bool(stream and stream.running and frame
                    and (utc_now() - frame.captured_at).total_seconds() <= 2)

    def current(self, camera):
        current = self.cameras.get(camera.id)
        return bool(current and current.enabled and fingerprint(current) == fingerprint(camera))

    def blocked(self, camera_id):
        return (self.global_write_failure or len(self.ingress.pending) >= self.ingress.capacity
                or camera_id in self.failed_cameras or any(j.camera_id in (None, camera_id) for j in self.retained.values())
                or self.durable_status.get("unfinished", 0) >= self.ingress.capacity)

    def submit(self, job):
        if not self.ingress.submit(job):
            # At most one in-flight general/fire pass and two review kinds per
            # camera can finish after admission closes. Bound their retained work.
            if len(self.retained) >= max(32, len(self.cameras) * 32):
                raise RuntimeError("Critical retained work capacity exceeded; processing stopped")
            self.retained[job.key] = job

    async def start(self):
        def recover():
            with session_scope() as session:
                for row in session.scalars(select(models.BackgroundTask).where(
                    models.BackgroundTask.kind == "review_audit", models.BackgroundTask.status != "completed")):
                    row.status = "completed"
                    row.result_json = json.dumps({"stale": True, "reason": "process_restarted"})
                    row.updated_at = utc_now()
                for delivery in session.scalars(select(models.WebhookDelivery).where(models.WebhookDelivery.status == "pending")):
                    key = f"notification:{delivery.id}"
                    if session.scalar(select(models.BackgroundTask.id).where(models.BackgroundTask.idempotency_key == key)) is None:
                        session.add(models.BackgroundTask(id=str(uuid4()), idempotency_key=key, kind="notification",
                            payload_json=json.dumps({"delivery_id": delivery.id}), status="pending", attempts=0,
                            retry_at=utc_now()))
        await self.io.run(recover)
        if hasattr(self.original, "traffic_summary"):
            summary = await self.io.run(self.original.traffic_summary)
            self.flow_counts = {row["camera_id"]: int(row.get("entered_today", 0)) for row in summary["cameras"]}
        self.ingress.start()
        self.reviews.start()
        self.tasks = [asyncio.create_task(self._writes(), name="business-outbox-worker"),
                      asyncio.create_task(self._maintenance(), name="postprocess-maintenance")]
        self.tasks += [asyncio.create_task(self._notifications(), name=f"notification-worker-{i}")
                       for i in range(self.runtime.settings.notification_workers)]

    async def close(self):
        await self.reviews.close()
        self._flush_flow()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # Drain accepted work before normal shutdown; durable tasks recover later.
        deadline = time.monotonic() + 10
        while (self.retained or self.ingress.pending) and time.monotonic() < deadline:
            self._admit_retained()
            await asyncio.sleep(0.05)
        left = await self.ingress.close()
        if left or self.retained:
            logger.error("Shutdown has unpersisted critical work: ingress=%s retained=%s", left, len(self.retained))

    def _admit_retained(self):
        for key, job in list(self.retained.items()):
            if not self.ingress.submit(job):
                break
            self.retained.pop(key)

    def _flush_flow(self):
        # One durable transaction for up to 100 camera/bucket entries. Current
        # values replace; increments accumulate until that transaction commits.
        keys = list(self.flow_pending)[:100]
        if keys:
            self.submit(Job.make("traffic_write", f"traffic:{uuid4()}",
                                 {"rows": [dict(self.flow_pending[key]) for key in keys]}))
            for key in keys:
                self.flow_pending.pop(key)

    def status(self):
        return {"enabled": True, "config_version": self.version, "writes": self.ingress.status(),
                "retained": len(self.retained), "durable": self.durable_status,
                "reviews": self.reviews.status(), "stale_results": self.stale_results,
                "review_last_result": self.review_last, "worker_errors": dict(self.worker_errors),
                "blocked_cameras": sorted(self.failed_cameras | {j.camera_id for j in self.retained.values() if j.camera_id}),
                "degraded": bool(self.retained or self.global_write_failure or self.failed_cameras
                    or self.reviews.unapplied or self.worker_errors or self.ingress.status()["degraded"]
                    or any(r["status"] == "failed" for r in self.durable_status.get("queues", [])))}

    async def _maintenance(self):
        while True:
            try:
                self._admit_retained()
                if not self.retained and len(self.ingress.pending) < self.ingress.capacity:
                    self._flush_flow()
                def stats():
                    with session_scope() as session:
                        rows = session.execute(select(models.BackgroundTask.kind, models.BackgroundTask.status,
                            func.count(), func.min(models.BackgroundTask.created_at)).where(
                            models.BackgroundTask.status != "completed").group_by(models.BackgroundTask.kind, models.BackgroundTask.status)).all()
                        failed = set(session.scalars(select(models.BackgroundTask.camera_id).where(
                            models.BackgroundTask.status == "failed", models.BackgroundTask.kind.in_(self.WRITE_KINDS))))
                    return [{"kind": kind, "status": status, "count": count,
                             "oldest_age_seconds": max(0, (utc_now() - created.replace(tzinfo=timezone.utc)).total_seconds())}
                            for kind, status, count, created in rows], failed
                rows, failed = await self.io.run(stats)
                self.durable_status = {"unfinished": sum(r["count"] for r in rows), "queues": rows}
                self.failed_cameras = failed - {None}
                self.global_write_failure = None in failed
                self.worker_errors.pop("maintenance", None)
            except Exception as exc:
                self.worker_errors["maintenance"] = type(exc).__name__
            await asyncio.sleep(1)

    def schedule_review(self, kind, camera, jpeg, now, event_started_at=None, modes=None,
                        off_duty_local_confirmed=False):
        if self.blocked(camera.id):
            return False
        ref = f"review-{uuid4().hex}.jpg"
        request = ReviewRequest(camera.id, kind, event_started_at.isoformat() if event_started_at else "",
                                self.version, now.isoformat(), ref, json.dumps({
                                    "modes": sorted(m.value for m in (modes or [])),
                                    "off_duty_local_confirmed": off_duty_local_confirmed,
                                    "camera_fingerprint": fingerprint(camera),
                                }))
        key = camera.id, kind
        previous = self.reviews.pending.get(key)
        if not self.reviews.submit(request):
            return False
        if previous:
            self.review_evidence.pop(previous[0].evidence_ref, None)
        self.review_evidence[ref] = jpeg
        return True

    def review_invalid(self, camera_id, mode):
        request = REVIEW.get()
        if request is None:
            return None
        camera = self.cameras.get(camera_id)
        now = utc_now()
        sampled = datetime.fromisoformat(request.sampled_at)
        if request.config_version != self.version:
            return "configuration_changed"
        if not camera or not camera.enabled or not self.online(camera_id):
            return "camera_unavailable"
        if (now - sampled).total_seconds() > self.runtime.settings.review_max_evidence_age_seconds:
            return "evidence_expired"
        if not is_scheduled(ScheduleSpec.model_validate(from_json(camera.schedule_json, {})), now):
            return "schedule_ended"
        previous = self.review_applied.get((camera_id, mode))
        if previous and sampled <= previous:
            return "out_of_order"
        if mode == Mode.OFF_DUTY.value:
            state = self.runtime.rules.for_camera(camera_id)
            event = state.absence_since.isoformat() if state.absence_since else ""
            if event != request.event_id or not state.absence_alerted:
                return "event_ended_or_person_returned"
        self.review_applied[camera_id, mode] = sampled
        return None

    async def _review(self, request):
        camera = self.repository.get_camera(request.camera_id)
        evidence = self.review_evidence[request.evidence_ref]
        # Persist evidence and request before the network call. A restart will
        # audit unresolved requests as stale, never resurrect in-memory events.
        await self.io.run(atomic_evidence, self.runtime.settings.evidence_dir, request.evidence_ref, evidence)
        job = Job.make("review_audit", f"review:{request.evidence_ref}", {
            "camera_id": request.camera_id, "kind": request.kind, "sampled_at": request.sampled_at,
            "event_id": request.event_id, "config_version": request.config_version,
        }, request.camera_id, request.evidence_ref)
        await self.io.run(self.store.persist, job)
        if request.config_version != self.version or not camera or not camera.enabled:
            return {"stale": True, "reason": "configuration_changed"}
        if (utc_now() - datetime.fromisoformat(request.sampled_at)).total_seconds() > self.runtime.settings.review_max_evidence_age_seconds:
            self.stale_results += 1
            return {"stale": True, "reason": "evidence_expired_before_dispatch"}
        token = REVIEW.set(request)
        try:
            now = datetime.fromisoformat(request.sampled_at)
            event = datetime.fromisoformat(request.event_id) if request.event_id else None
            if request.kind == "off_duty":
                result = await self.runtime._review_off_duty(camera, evidence, event, now)
            else:
                payload = json.loads(request.payload_json)
                result = await self.runtime._behaviors(camera, {Mode(m) for m in payload["modes"]}, evidence,
                    CameraOptions.model_validate(from_json(camera.options_json, {})),
                    ScheduleSpec.model_validate(from_json(camera.schedule_json, {})), now,
                    payload["off_duty_local_confirmed"], event)
            return {"result": result}
        finally:
            REVIEW.reset(token)

    async def _review_completed(self, request, result, error):
        self.review_last[request.camera_id] = utc_now().isoformat()
        if error:
            self.repository.add_analysis(camera_id=request.camera_id, mode=request.kind,
                status="uncertain", confidence=0, reason="后台复核超时或失败", error=type(error).__name__)
        def finish():
            with session_scope() as session:
                row = session.scalar(select(models.BackgroundTask).where(
                    models.BackgroundTask.idempotency_key == f"review:{request.evidence_ref}").with_for_update())
                if row:
                    row.status = "completed"
                    row.result_json = json.dumps(result or {"error": type(error).__name__})
                    row.updated_at = utc_now()
        await self.io.run(finish)
        self.review_evidence.pop(request.evidence_ref, None)

    def enqueue_alert(self, camera, analysis, evidence_items, reason, confidence, cooldown_seconds,
                      event_phase=None, event_started_at=None, event_ended_at=None,
                      directory_cooldown=False, bypass_cooldown=False):
        members = {}
        if analysis.mode == Mode.OFF_DUTY.value:
            for item in evidence_items:
                state = self.runtime.rules.for_camera(item["camera_id"])
                members[item["camera_id"]] = state.absence_since.isoformat() if state.absence_since else ""
        subject = str(camera.directory_id) if directory_cooldown else camera.id
        cooldown_key = (directory_cooldown, subject, analysis.mode)
        last = self.alert_cooldowns.get(cooldown_key)
        if not bypass_cooldown and last and (utc_now() - last).total_seconds() < cooldown_seconds:
            return
        identity = [subject, analysis.mode, members or (event_started_at.isoformat() if event_started_at else analysis.id)]
        key = "alert:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if key in self.reserved_alerts:
            return
        self.reserved_alerts.add(key)
        directory = self.directories.get(camera.directory_id)
        self.submit(Job.make("alert_write", key, {
            "camera_id": camera.id, "camera_fingerprint": fingerprint(camera),
            "analysis_job_id": analysis.id, "mode": analysis.mode,
            "directory_id": camera.directory_id,
            "alert_name": f"{directory['name']}营业厅视频" if directory else "未分组营业厅视频",
            "reason": reason, "confidence": confidence, "cooldown_seconds": cooldown_seconds,
            "directory_cooldown": directory_cooldown, "bypass_cooldown": bypass_cooldown,
            "members": members, "event_phase": event_phase,
            "event_started_at": event_started_at.isoformat() if event_started_at else None,
            "event_ended_at": event_ended_at.isoformat() if event_ended_at else None,
            "evidences": [{**{k: v for k, v in item.items() if k != "jpeg"},
                           "jpeg_b64": base64.b64encode(item["jpeg"]).decode()} for item in evidence_items],
        }, camera.id))

    def alert_valid(self, payload):
        camera = self.cameras.get(payload["camera_id"])
        if not camera or not camera.enabled or fingerprint(camera) != payload["camera_fingerprint"]:
            return False
        if payload["mode"] == Mode.OFF_DUTY.value:
            members = {c.id for c in self.cameras.values() if c.enabled and c.directory_id == camera.directory_id
                       and Mode.OFF_DUTY.value in from_json(c.modes_json, [])}
            if members != set(payload["members"]):
                return False
            for camera_id, event_id in payload["members"].items():
                state = self.runtime.rules.for_camera(camera_id)
                if (not self.online(camera_id) or not state.absence_vlm_confirmed or not state.absence_alerted
                    or not state.absence_since or state.absence_since.isoformat() != event_id
                    or not is_scheduled(ScheduleSpec.model_validate(from_json(self.cameras[camera_id].schedule_json, {})), utc_now())):
                    return False
        return True

    async def _writes(self):
        # A single transaction writer preserves traffic bucket/current ordering;
        # model and notification workers are independent and never hold its lock.
        while True:
            claim = None
            try:
                batch = await self.io.run(self.store.claim_many,
                    ["analysis_write", "traffic_write", "model_log_write"])
                if batch:
                    try:
                        await self.io.run(self.store.complete_many, batch, self._write_transaction)
                    except Exception:
                        # A bad record must not poison other records in its batch.
                        # The failed batch rolled back; isolate the records safely.
                        for item in batch:
                            try:
                                await self.io.run(self.store.complete, item["id"], item["lease_token"],
                                    lambda session, payload: self._write_transaction(session, item["kind"], payload))
                            except Exception as exc:
                                await self.io.run(self.store.fail, item["id"], item["lease_token"], type(exc).__name__)
                claim = await self.io.run(self.store.claim, ["alert_write"], 120)
                if not claim:
                    await asyncio.sleep(0.05)
                    continue
                payload = json.loads(claim["payload_json"])
                valid = True
                if claim["kind"] == "alert_write":
                    valid = self.alert_valid(payload)
                    if valid:
                        for index, item in enumerate(payload["evidences"]):
                            item["evidence_path"] = f"alert-{claim['id']}-{index}.jpg"
                            await self.io.run(atomic_evidence, self.runtime.settings.evidence_dir,
                                             item["evidence_path"], base64.b64decode(item["jpeg_b64"]))
                        valid = self.alert_valid(payload)
                outcome = {}
                def handler(session, _):
                    result = self._write_transaction(session, claim["kind"], payload, valid)
                    outcome.update(result)
                    return result
                committed = await self.io.run(self.store.complete, claim["id"], claim["lease_token"], handler)
                if committed and claim["kind"] == "alert_write":
                    self.reserved_alerts.discard(claim["key"])
                    subject = str(payload["directory_id"]) if payload["directory_cooldown"] else payload["camera_id"]
                    if outcome.get("alert_id") or outcome.get("cooldown_at"):
                        self.alert_cooldowns[payload["directory_cooldown"], subject, payload["mode"]] = (
                            datetime.fromisoformat(outcome["cooldown_at"]).replace(tzinfo=timezone.utc)
                            if outcome.get("cooldown_at") else utc_now())
                    if outcome.get("alert_id"):
                        self.cooldowns[payload["directory_id"], payload["mode"]] = utc_now()
                        # Do not reset a newer event created while the transaction ran.
                        for camera_id, event_id in payload["members"].items():
                            state = self.runtime.rules.for_camera(camera_id)
                            if state.absence_since and state.absence_since.isoformat() == event_id:
                                state.start_new_off_duty_cycle(utc_now())
                        event_payload = await self.io.run(lambda: self.runtime.alerts._payload(
                            self.original.get_alert(outcome["alert_id"])))
                        await self.runtime.event_bus.publish(event_payload)
                self.worker_errors.pop("writes", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.worker_errors["writes"] = type(exc).__name__
                if claim:
                    try:
                        await self.io.run(self.store.fail, claim["id"], claim["lease_token"], type(exc).__name__)
                    except Exception:
                        pass  # Lease recovery retains the durable work.
                await asyncio.sleep(1)

    def _write_transaction(self, session, kind, payload, valid=True):
        if kind == "analysis_write":
            values = db_values(payload)
            if values.get("camera_id") and session.get(models.Camera, values["camera_id"]) is None:
                values["camera_id"] = None
            row = models.Analysis(**values)
            session.add(row)
            session.flush()
            return {"analysis_id": row.id}
        if kind == "model_log_write":
            values = db_values(payload)
            if values.get("camera_id") and session.get(models.Camera, values["camera_id"]) is None:
                values["camera_id"] = None
            session.add(models.ModelCallLog(**values))
            return {}
        if kind == "traffic_write":
            if "rows" in payload:
                for item in payload["rows"]:
                    self._write_transaction(session, kind, item)
                return {}
            if session.get(models.Camera, payload["camera_id"]) is None:
                return {"skipped": "camera_deleted"}
            bucket = datetime.fromisoformat(payload["bucket_start"])
            row = session.scalar(select(models.TrafficAggregate).where(
                models.TrafficAggregate.camera_id == payload["camera_id"], models.TrafficAggregate.bucket_start == bucket).with_for_update())
            if row is None:
                row = models.TrafficAggregate(camera_id=payload["camera_id"], bucket_start=bucket, entered=0, exited=0)
                session.add(row)
            row.current_count = payload["current_count"]
            row.entered += payload["entered"]
            row.exited += payload["exited"]
            return {}
        if not valid:
            return {"stale": True}
        camera = session.get(models.Camera, payload["camera_id"])
        if not camera or not camera.enabled or fingerprint(camera) != payload["camera_fingerprint"]:
            return {"stale": True}
        subject = (models.Alert.directory_id == payload["directory_id"] if payload["directory_cooldown"]
                   else models.Alert.camera_id == payload["camera_id"])
        last = session.scalar(select(func.max(models.Alert.created_at)).where(subject, models.Alert.mode == payload["mode"]))
        if not payload["bypass_cooldown"] and last and (utc_now() - last.replace(tzinfo=timezone.utc)).total_seconds() < payload["cooldown_seconds"]:
            return {"skipped": "cooldown", "cooldown_at": last.isoformat()}
        analysis_job = session.get(models.BackgroundTask, payload["analysis_job_id"])
        if analysis_job is None or analysis_job.status != "completed":
            raise RuntimeError("Analysis is not persisted yet")
        analysis = session.get(models.Analysis, json.loads(analysis_job.result_json)["analysis_id"])
        row = models.Alert(camera_id=camera.id, directory_id=payload["directory_id"], alert_name=payload["alert_name"],
            analysis_id=analysis.id, mode=analysis.mode, status="confirmed", confidence=payload["confidence"],
            reason=payload["reason"], severity=analysis.severity, zone_name=analysis.zone_name,
            local_model=analysis.local_model, model_version=analysis.model_version,
            evidence_path=payload["evidences"][0]["evidence_path"], webhook_status="not_sent", shadow=False,
            event_phase=payload["event_phase"], event_started_at=datetime.fromisoformat(payload["event_started_at"]) if payload["event_started_at"] else None,
            event_ended_at=datetime.fromisoformat(payload["event_ended_at"]) if payload["event_ended_at"] else None)
        session.add(row)
        session.flush()
        for index, item in enumerate(payload["evidences"]):
            session.add(models.AlertEvidence(alert_id=row.id, camera_id=item["camera_id"], camera_name=item["camera_name"],
                evidence_path=item["evidence_path"], confidence=item.get("confidence", 0), sort_order=index))
        for target in session.scalars(select(models.WebhookTarget).where(models.WebhookTarget.enabled.is_(True))):
            if target.url and row.severity in from_json(target.auto_severities_json, []):
                delivery = models.WebhookDelivery(alert_id=row.id, webhook_target_id=target.id,
                    target_name=target.name, target_url=target.url, trigger="automatic", status="pending")
                session.add(delivery)
                session.flush()
                session.add(models.BackgroundTask(id=str(uuid4()), idempotency_key=f"notification:{delivery.id}",
                    kind="notification", camera_id=camera.id, payload_json=json.dumps({"delivery_id": delivery.id}),
                    status="pending", attempts=0, retry_at=utc_now()))
                row.webhook_status = "pending"
        return {"alert_id": row.id}

    async def _notifications(self):
        while True:
            claim = None
            try:
                claim = await self.io.run(self.store.claim, ["notification"], 120)
                if not claim:
                    await asyncio.sleep(0.5)
                    continue
                delivery_id = json.loads(claim["payload_json"])["delivery_id"]
                def load():
                    with session_scope() as session:
                        delivery = session.get(models.WebhookDelivery, delivery_id)
                        if not delivery:
                            return None
                        alert = session.get(models.Alert, delivery.alert_id)
                        if not alert:
                            return None
                        return delivery.target_url, delivery.alert_id, self.runtime.alerts._payload(alert)
                data = await self.io.run(load)
                if data:
                    url, alert_id, payload = data
                    paths = [self.runtime.settings.evidence_dir / value.rsplit("/", 1)[-1] for value in payload["evidence_urls"]]
                    await asyncio.wait_for(self.runtime.webhook.send(url, payload, paths, attempts=1), 60)
                def complete(session, _):
                    delivery = session.get(models.WebhookDelivery, delivery_id)
                    if delivery:
                        delivery.status, delivery.error = "delivered", None
                        delivery.updated_at = utc_now()
                        session.flush()
                        rows = list(session.scalars(select(models.WebhookDelivery).where(models.WebhookDelivery.alert_id == delivery.alert_id)))
                        alert = session.get(models.Alert, delivery.alert_id)
                        if alert:
                            alert.webhook_status = "delivered" if all(r.status == "delivered" for r in rows) else "pending"
                    return {"delivered": bool(data)}
                await self.io.run(self.store.complete, claim["id"], claim["lease_token"], complete)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if claim:
                    try:
                        await self.io.run(self.store.fail, claim["id"], claim["lease_token"], type(exc).__name__)
                        def failed():
                            with session_scope() as session:
                                delivery = session.get(models.WebhookDelivery, json.loads(claim["payload_json"])["delivery_id"])
                                if delivery:
                                    delivery.status, delivery.error = "failed", type(exc).__name__
                                    alert = session.get(models.Alert, delivery.alert_id)
                                    if alert:
                                        alert.webhook_status = "failed"
                        await self.io.run(failed)
                    except Exception:
                        pass
                await asyncio.sleep(1)
