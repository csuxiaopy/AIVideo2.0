import asyncio
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.background import CriticalIngress, IOPool, Job, LatestWriter, ReviewQueue, ReviewRequest, ReviewValidity, atomic_evidence
from backend.database import Base, utc_now
from backend.models import BackgroundTask
from backend.task_store import TaskStore


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    BackgroundTask.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def sessions():
        with factory.begin() as session:
            yield session

    yield TaskStore(sessions)
    engine.dispose()


def test_idempotent_insert_and_conflicting_key(store):
    first = Job.make("analysis", "camera:event", {"value": 1}, "camera")
    assert store.persist(first) == first.id
    assert store.persist(Job.make("analysis", first.key, {"value": 1}, "camera")) == first.id
    with pytest.raises(ValueError):
        store.persist(Job.make("analysis", first.key, {"value": 2}, "camera"))


def test_effects_and_completion_are_atomic(store):
    job = Job.make("analysis", "one", {"value": 2})
    store.persist(job)
    claimed = store.claim(["analysis"])

    def failing(session, payload):
        session.add(BackgroundTask(id="effect", idempotency_key="effect", kind="effect",
                                   payload_json="{}"))
        raise OSError("injected database failure")

    with pytest.raises(OSError):
        store.complete(job.id, claimed["lease_token"], failing)
    assert store.get("effect") is None
    assert store.get(job.id)["status"] == "running"
    assert store.complete(job.id, claimed["lease_token"], lambda session, payload: payload)
    assert not store.complete(job.id, claimed["lease_token"], failing)
    assert store.get(job.id)["result"] == {"value": 2}


def test_restart_recovers_expired_lease_and_rejects_old_owner(store):
    job = Job.make("notification", "one", {})
    store.persist(job)
    first = store.claim(["notification"])
    assert store.claim(["notification"]) is None
    with store.sessions() as session:
        session.get(BackgroundTask, job.id).lease_until = utc_now() - timedelta(seconds=1)
    restarted = TaskStore(store.sessions)
    second = restarted.claim(["notification"])
    assert second["lease_token"] != first["lease_token"]
    assert not store.complete(job.id, first["lease_token"], lambda *_: {})
    assert not store.renew(job.id, first["lease_token"])
    assert not store.fail(job.id, first["lease_token"], "late failure")
    assert store.complete(job.id, second["lease_token"], lambda *_: {})


def test_bounded_retry_and_manual_recovery(store):
    job = Job.make("notification", "one", {})
    store.persist(job)
    claimed = store.claim(["notification"])
    assert store.fail(job.id, claimed["lease_token"], "timeout", max_attempts=1)
    assert store.get(job.id)["status"] == "failed"
    assert store.claim(["notification"]) is None
    assert store.retry(job.id)
    assert store.claim(["notification"]) is not None


def test_public_status_omits_payload_and_evidence(store):
    job = Job.make("notification", "secret", {"url": "https://private/?token=secret"}, evidence_ref="internal.jpg")
    store.persist(job)
    assert "secret" not in json.dumps(store.get(job.id))
    assert "internal.jpg" not in json.dumps(store.get(job.id))


def test_batch_persist_claim_and_complete_are_idempotent(store):
    jobs = [Job.make("test", f"key-{i}", {"value": i}) for i in range(10)]
    assert store.persist_many(jobs) == [job.id for job in jobs]
    assert store.persist_many(jobs) == [job.id for job in jobs]
    claims = store.claim_many(["test"])
    assert len(claims) == 10
    assert store.claim_many(["test"]) == []
    assert store.complete_many(claims, lambda session, kind, payload: payload) == 10
    assert store.complete_many(claims, lambda *_: pytest.fail("duplicate effects")) == 0


def test_failed_completion_batch_rolls_back_all_tasks(store):
    jobs = [Job.make("test", f"key-{i}", {"value": i}) for i in range(3)]
    store.persist_many(jobs)
    claims = store.claim_many(["test"])
    def handler(session, kind, payload):
        if payload["value"] == 2:
            raise OSError("injected")
        return payload
    with pytest.raises(OSError):
        store.complete_many(claims, handler)
    assert all(store.get(job.id)["status"] == "running" for job in jobs)


@pytest.mark.asyncio
async def test_critical_full_and_failed_write_retain_work():
    io = IOPool(1)
    failures = True
    persisted = []

    def persist(job):
        if failures:
            raise OSError("injected")
        persisted.append(job.key)
        return job.id

    queue = CriticalIngress(io, persist, 1)
    one, two = Job.make("analysis", "1", {}), Job.make("analysis", "2", {})
    assert queue.submit(one)
    assert queue.submit(one)
    assert not queue.submit(two)
    assert not await queue.flush_one()
    assert list(queue.pending) == ["1"]
    assert queue.status()["degraded"]
    failures = False
    assert await queue.flush_one()
    assert queue.submit(two)
    assert await queue.flush_one()
    assert persisted == ["1", "2"]
    assert await queue.close() == 0
    await io.close()


@pytest.mark.asyncio
async def test_slow_status_writer_does_not_erase_newer_value():
    io = IOPool(1)
    started, release = threading.Event(), threading.Event()
    batches = []

    def write(batch):
        batches.append(batch)
        started.set()
        assert release.wait(5)

    writer = LatestWriter(io, write, 2)
    writer.submit("a", {"seq": 1})
    flushing = asyncio.create_task(writer.flush())
    while not started.is_set():
        await asyncio.sleep(0.001)
    assert writer.submit("a", {"seq": 2})
    assert writer.submit("b", {"seq": 1})
    assert not writer.submit("c", {})
    release.set()
    await flushing
    assert "a" in writer.pending
    await writer.flush()
    assert not writer.pending
    assert dict(batches[1])["a"]["seq"] == 2
    await io.close()


@pytest.mark.asyncio
async def test_status_failures_remain_visible_and_retry_latest():
    io = IOPool(1)

    def fail(batch):
        raise OSError("failure")

    writer = LatestWriter(io, fail, 2)
    writer.submit("a", {"seq": 1})
    await writer.flush()
    assert writer.error == "OSError" and writer.pending
    writer.submit("a", {"seq": 2})
    saved = []
    writer.write_batch = saved.extend
    await writer.flush()
    assert saved == [("a", {"seq": 2})]
    assert writer.error is None
    await io.close()


@pytest.mark.asyncio
async def test_review_concurrency_latest_candidate_and_other_camera_progress():
    release = asyncio.Event()
    started = []
    completed = []

    async def handler(request):
        started.append((request.camera_id, request.evidence_ref))
        if request.evidence_ref == "first":
            await release.wait()
        return request.evidence_ref

    async def completion(request, result, error):
        completed.append((request.camera_id, result, error))

    queue = ReviewQueue(handler, completion, concurrency=2, max_keys=2)
    request = ReviewRequest("a", "off_duty", "event", 1, "now", "first")
    queue.submit(request)
    queue.start()
    for _ in range(100):
        if queue.active:
            break
        await asyncio.sleep(0.001)
    assert queue.submit(replace(request, evidence_ref="old"))
    assert queue.submit(replace(request, evidence_ref="latest"))
    assert queue.submit(replace(request, camera_id="b", evidence_ref="b"))
    assert not queue.submit(replace(request, camera_id="c"))
    for _ in range(100):
        if completed:
            break
        await asyncio.sleep(0.001)
    assert completed == [("b", "b", None)]
    release.set()
    for _ in range(100):
        if len(completed) == 3:
            break
        await asyncio.sleep(0.001)
    assert ("a", "old") not in started
    assert ("a", "latest") in started
    await queue.close()


@pytest.mark.asyncio
async def test_review_timeout_is_failure_not_confirmation():
    results = []

    async def handler(request):
        await asyncio.sleep(30)

    async def completion(request, result, error):
        results.append((result, error))

    queue = ReviewQueue(handler, completion, timeout=0.01)
    queue.submit(ReviewRequest("a", "off_duty", "e", 1, "now", "a.jpg"))
    queue.start()
    for _ in range(100):
        if results:
            break
        await asyncio.sleep(0.002)
    assert results[0][0] is None
    assert isinstance(results[0][1], TimeoutError)
    assert queue.failures == 1
    await queue.close()


def test_atomic_evidence_and_path_validation(tmp_path):
    atomic_evidence(tmp_path, "event.jpg", b"first")
    atomic_evidence(tmp_path, "event.jpg", b"second")
    assert (tmp_path / "event.jpg").read_bytes() == b"second"
    assert list(tmp_path.iterdir()) == [tmp_path / "event.jpg"]
    for name in ("../escape.jpg", "..\\escape.jpg", "", "."):
        with pytest.raises(ValueError):
            atomic_evidence(tmp_path, name, b"invalid")


@pytest.mark.parametrize("change,reason", [
    ({"config_version": 2}, "configuration_changed"),
    ({"event_id": "new"}, "event_ended"),
    ({"event_active": False}, "event_ended"),
    ({"enabled": False}, "camera_unavailable"),
    ({"online": False}, "camera_unavailable"),
    ({"scheduled": False}, "schedule_ended"),
    ({"occupied": True}, "person_returned"),
    ({"latest_frame_at": None}, "frame_unavailable"),
])
def test_stale_review_gate(change, reason):
    now = utc_now()
    request = ReviewRequest("a", "off_duty", "old", 1, now.isoformat(), "a.jpg")
    state = ReviewValidity("old", 1, True, True, True, True, now)
    assert state.rejection_reason(request, now, 2, 60) is None
    assert replace(state, **change).rejection_reason(request, now, 2, 60) == reason


def test_review_evidence_and_latest_frame_have_separate_expirations():
    now = utc_now()
    request = ReviewRequest("a", "off_duty", "e", 1, (now - timedelta(seconds=30)).isoformat(), "a.jpg")
    state = ReviewValidity("e", 1, True, True, True, True, now)
    assert state.rejection_reason(request, now, 2, 60) is None
    assert state.rejection_reason(request, now, 2, 20) == "evidence_expired"
    assert replace(state, latest_frame_at=now - timedelta(seconds=3)).rejection_reason(request, now, 2, 60) == "frame_expired"


@pytest.mark.asyncio
async def test_failed_completion_retains_result_and_manual_retry_does_not_call_model():
    calls = []
    saved = []
    fail = True

    async def handler(request):
        calls.append(request)
        return "confirmed"

    async def completion(request, result, error):
        if fail:
            raise OSError("storage down")
        saved.append(result)

    queue = ReviewQueue(handler, completion)
    queue.submit(ReviewRequest("a", "off_duty", "e", 1, "now", "a.jpg"))
    queue.start()
    for _ in range(100):
        if queue.unapplied:
            break
        await asyncio.sleep(0.002)
    assert queue.status()["unapplied_results"] == 1
    fail = False
    assert await queue.retry_completion("a", "off_duty")
    assert saved == ["confirmed"]
    assert len(calls) == 1
    await queue.close()
