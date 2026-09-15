import asyncio
import json
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.alerts import AlertService
from backend.async_pipeline import AsyncPipeline, CameraSnapshot, REVIEW, fingerprint
from backend.background import IOPool, Job, ReviewRequest
from backend.config import Settings
from backend.database import Base, utc_now
from backend.eventbus import EventBus
from backend.pipeline import MonitoringRuntime
from backend.rules import RuleStateRegistry
from backend.schemas import Mode, VLMResult
from backend.task_store import TaskStore
from backend.vlm import VLMResponse


def camera(camera_id="a"):
    return CameraSnapshot(camera_id, camera_id, True, None, '["off_duty"]', '{}', '{}', '{}',
        '{"off_duty_seconds":60,"shift_grace_seconds":0}', "encrypted", None, 1, True)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def sessions():
        with factory.begin() as session:
            yield session

    monkeypatch.setattr("backend.async_pipeline.session_scope", sessions)
    yield sessions
    engine.dispose()


def setup_runtime(tmp_path, db):
    runtime = MonitoringRuntime.__new__(MonitoringRuntime)
    runtime.settings = Settings(_env_file=None, evidence_dir=tmp_path, async_postprocessing=True)
    runtime.rules = RuleStateRegistry()
    runtime.last_mode_run = defaultdict(float)
    runtime.last_detector_error = defaultdict(float)
    runtime.directory_locks = defaultdict(asyncio.Lock)
    runtime.event_bus = EventBus()
    runtime.yolo = SimpleNamespace(model_name="test", detect=lambda *_: [], people=lambda x: x)
    ok, image = cv2.imencode(".jpg", np.full((80, 120, 3), 120, dtype=np.uint8))
    frame = SimpleNamespace(jpeg=image.tobytes(), captured_at=utc_now(), sequence=1)
    runtime.media = SimpleNamespace(latest=lambda _: frame,
        capture_streams={"a": SimpleNamespace(running=True), "b": SimpleNamespace(running=True)},
        set_person_detections=lambda *_: None, set_object_detections=lambda *_: None,
        set_intrusion=lambda *_: None)
    runtime.webhook = SimpleNamespace(send=AsyncMock())
    io = IOPool(4)
    post = AsyncPipeline(runtime, SimpleNamespace(), io)
    post.store = TaskStore(db)
    post.ingress.persist = post.store.persist
    post.cameras = {"a": camera(), "b": camera("b")}
    post.version = 1
    runtime.async_pipeline = post
    runtime.repository = post.repository
    runtime.alerts = AlertService(runtime.settings, SimpleNamespace(), None, runtime.event_bus, runtime.webhook)
    runtime.alerts.async_pipeline = post
    with db() as session:
        for item in post.cameras.values():
            values = {k: getattr(item, k) for k in CameraSnapshot.__dataclass_fields__}
            session.add(models.Camera(**values))
    return runtime, post, io, frame


def threshold(runtime, key="a"):
    now = utc_now()
    state = runtime.rules.for_camera(key)
    state.absence_event_update(False, True, 60, now - timedelta(seconds=61), schedule_key="always")
    state.absence_event_update(False, True, 60, now, schedule_key="always")
    return state.absence_since


def response():
    return VLMResponse(results={Mode.OFF_DUTY: VLMResult(mode=Mode.OFF_DUTY, status="confirmed", confidence=.9, reason="absent")},
        request_id="r", usage={}, latency_ms=30000, provider="test", model="test")


@pytest.mark.asyncio
async def test_local_detection_submits_review_without_waiting(tmp_path, db):
    runtime, post, io, frame = setup_runtime(tmp_path, db)
    # schedule_key from the always-on rule is 'always'; the local pass must
    # continue on both cameras while the independent model handler is blocked.
    entered = asyncio.Event()
    release = asyncio.Event()

    class VLM:
        async def analyze_off_duty(self, evidence):
            entered.set()
            await release.wait()
            return response()

    runtime.vlm = VLM()
    threshold(runtime)
    await asyncio.wait_for(runtime._process(camera()), 1)
    assert post.reviews.pending
    post.reviews.start()
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(runtime._process(camera("b")), 1)
    assert runtime.person_detection_status()["b"]["completed_in_window"] == 1
    release.set()
    await post.reviews.close()
    await io.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["return", "config", "offline", "disable", "out_of_order"])
async def test_late_review_only_audits_without_alert(tmp_path, db, invalidate):
    runtime, post, io, frame = setup_runtime(tmp_path, db)
    event = threshold(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    class VLM:
        async def analyze_off_duty(self, jpeg):
            entered.set()
            await release.wait()
            return response()

    runtime.vlm = VLM()
    now = utc_now()
    req = ReviewRequest("a", "off_duty", event.isoformat(), 1, now.isoformat(), "e.jpg")

    async def run():
        token = REVIEW.set(req)
        try:
            return await runtime._review_off_duty(camera(), b"evidence", event, now)
        finally:
            REVIEW.reset(token)

    task = asyncio.create_task(run())
    await entered.wait()
    if invalidate == "return":
        runtime.rules.for_camera("a").absence_event_update(True, True, 60, utc_now())
    elif invalidate == "config":
        post.version += 1
    elif invalidate == "offline":
        frame.captured_at = utc_now() - timedelta(seconds=10)
    elif invalidate == "disable":
        post.cameras["a"] = replace(camera(), enabled=False)
    else:
        post.review_applied["a", "off_duty"] = now + timedelta(seconds=1)
    release.set()
    result = await task
    assert result["status"] == "stale"
    assert not runtime.rules.for_camera("a").absence_vlm_confirmed
    jobs = [item[0] for item in post.ingress.pending.values()]
    assert [job.kind for job in jobs] == ["analysis_write"]
    assert json.loads(jobs[0].payload_json)["status"] == "stale"
    await io.close()


@pytest.mark.asyncio
async def test_flow_accumulates_and_task_retry_cannot_double_count(tmp_path, db):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    post.repository.upsert_traffic("a", 2, 1, 0)
    post.repository.upsert_traffic("a", 3, 2, 0)
    post._flush_flow()
    assert not post.flow_pending
    await post.ingress.flush_one()
    claim = post.store.claim(["traffic_write"])
    def handler(session, payload):
        return post._write_transaction(session, "traffic_write", payload)
    assert post.store.complete(claim["id"], claim["lease_token"], handler)
    assert not post.store.complete(claim["id"], claim["lease_token"], handler)
    with db() as session:
        row = session.scalar(select(models.TrafficAggregate))
        assert (row.current_count, row.entered) == (3, 3)
    await io.close()


@pytest.mark.asyncio
async def test_alert_and_notification_commit_together_and_dedupe(tmp_path, db):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    with db() as session:
        session.add(models.WebhookTarget(name="test", enabled=True, url="https://example.invalid", auto_severities_json='["high"]'))
    analysis = post.repository.add_analysis(camera_id="a", mode="phone_use", status="confirmed", confidence=.9, severity="high")
    await post.ingress.flush_one()
    claim = post.store.claim(["analysis_write"])
    post.store.complete(claim["id"], claim["lease_token"], lambda s,p: post._write_transaction(s,"analysis_write",p))
    post.enqueue_alert(camera(), analysis, [{"camera_id":"a", "camera_name":"A", "jpeg":b"jpeg"}], "reason", .9, 0)
    await post.ingress.flush_one()
    claim = post.store.claim(["alert_write"])
    payload = json.loads(claim["payload_json"])
    payload["evidences"][0]["evidence_path"] = "deterministic.jpg"
    handler = lambda s,p: post._write_transaction(s,"alert_write",payload)
    assert post.store.complete(claim["id"], claim["lease_token"], handler)
    assert not post.store.complete(claim["id"], claim["lease_token"], handler)
    with db() as session:
        assert session.scalar(select(func.count()).select_from(models.Alert)) == 1
        assert session.scalar(select(func.count()).select_from(models.WebhookDelivery)) == 1
        assert session.scalar(select(func.count()).select_from(models.BackgroundTask).where(models.BackgroundTask.kind=="notification")) == 1
    await io.close()


@pytest.mark.asyncio
async def test_full_ingress_retains_event_and_blocks_camera(tmp_path, db):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    post.ingress.capacity = 1
    post.repository.add_analysis(camera_id="a", mode="off_duty", status="none")
    post.repository.add_analysis(camera_id="b", mode="off_duty", status="none")
    assert len(post.ingress.pending) == 1 and len(post.retained) == 1
    assert post.blocked("b")
    await post.ingress.flush_one()
    post._admit_retained()
    assert not post.retained
    await post.ingress.flush_one()
    with db() as session:
        assert session.scalar(select(func.count()).select_from(models.BackgroundTask)) == 2
    await io.close()


@pytest.mark.asyncio
async def test_group_revalidated_after_member_return_or_membership_change(tmp_path, db):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    members = {}
    for key in ("a", "b"):
        event = threshold(runtime, key)
        runtime.rules.for_camera(key).record_off_duty_review(True, utc_now(), b"evidence", .9)
        members[key] = event.isoformat()
    payload = {"camera_id":"a", "camera_fingerprint":fingerprint(camera()), "mode":"off_duty", "members":members}
    assert post.alert_valid(payload)
    runtime.rules.for_camera("b").absence_event_update(True, True, 60, utc_now())
    assert not post.alert_valid(payload)
    post.cameras.pop("b")
    assert not post.alert_valid(payload)
    await io.close()


@pytest.mark.asyncio
async def test_thirty_second_vlm_delay_does_not_hold_detection_worker(tmp_path, db):
    runtime, post, io, frame = setup_runtime(tmp_path, db)
    entered = asyncio.Event()

    class SlowVLM:
        async def analyze_off_duty(self, evidence):
            entered.set()
            await asyncio.sleep(30)
            return response()

    runtime.vlm = SlowVLM()
    event = threshold(runtime)
    post.schedule_review("off_duty", camera(), frame.jpeg, utc_now(), event)
    post.reviews.start()
    await asyncio.wait_for(entered.wait(), 3)
    count = 0
    deadline = asyncio.get_running_loop().time() + 30.2
    while asyncio.get_running_loop().time() < deadline:
        frame.captured_at = utc_now()
        await asyncio.wait_for(runtime._process(camera("b")), 1)
        count += 1
        await asyncio.sleep(.1)
    assert count >= 100
    await post.reviews.close()
    await io.close()


@pytest.mark.asyncio
async def test_evidence_failure_leaves_durable_retry_without_alert(tmp_path, db, monkeypatch):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    analysis = post.repository.add_analysis(camera_id="a", mode="phone_use", status="confirmed")
    await post.ingress.flush_one()
    claim = post.store.claim(["analysis_write"])
    post.store.complete(claim["id"], claim["lease_token"], lambda s,p: post._write_transaction(s,"analysis_write",p))
    post.enqueue_alert(camera(), analysis, [{"camera_id":"a", "camera_name":"A", "jpeg":b"x"}], "reason", .9, 0)
    await post.ingress.flush_one()

    def fail(*args):
        raise OSError("injected storage failure")

    monkeypatch.setattr("backend.async_pipeline.atomic_evidence", fail)
    task = asyncio.create_task(post._writes())
    for _ in range(100):
        if post.worker_errors:
            break
        await asyncio.sleep(.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    with db() as session:
        assert session.scalar(select(func.count()).select_from(models.Alert)) == 0
        row = session.scalar(select(models.BackgroundTask).where(models.BackgroundTask.kind=="alert_write"))
        assert row.status in {"retry", "running"}
    await io.close()
