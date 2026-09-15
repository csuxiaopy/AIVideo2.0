"""Run with the NEW backend mounted and a dedicated monitor_async_test_* DB.

No production data, models, RTSP connections or external notifications are used.
"""
import json
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import func, select

from backend import models
from backend.async_pipeline import AsyncPipeline, fingerprint
from backend.background import Job
from backend.database import Base, engine, session_scope, settings, utc_now
from backend.task_store import TaskStore

assert urlsplit(settings.database_url).path.startswith("/monitor_async_test_"), "Refusing non-test database"
Base.metadata.create_all(engine)
store = TaskStore()
post = AsyncPipeline.__new__(AsyncPipeline)
camera_id = "test-" + uuid4().hex
with session_scope() as session:
    camera = models.Camera(id=camera_id, name="test", rtsp_url_encrypted="not-a-url", enabled=True)
    session.add(camera)
    session.add(models.WebhookTarget(name="test", enabled=True, url="https://example.invalid",
                                     auto_severities_json='["high"]'))
    session.flush()
    camera_fingerprint = fingerprint(camera)


def execute(job):
    store.persist(job)
    claim = store.claim([job.kind])
    assert claim and claim["id"] == job.id
    fn = lambda s,p: post._write_transaction(s, job.kind, p)
    assert store.complete(job.id, claim["lease_token"], fn)
    assert not store.complete(job.id, claim["lease_token"], fn)
    return store.get(job.id)["result"]


traffic = Job.make("traffic_write", str(uuid4()), {"rows": [
    {"camera_id":camera_id, "bucket_start":utc_now().replace(second=0,microsecond=0).isoformat(),
     "current_count":4, "entered":3, "exited":0},
]}, camera_id)
execute(traffic)
assert store.persist(traffic) == traffic.id
with session_scope() as session:
    row = session.scalar(select(models.TrafficAggregate).where(models.TrafficAggregate.camera_id==camera_id))
    assert row.entered == 3 and row.current_count == 4

analysis = Job.make("analysis_write", str(uuid4()), {"camera_id":camera_id, "camera_name":"test",
    "mode":"phone_use", "status":"confirmed", "confidence":.9, "severity":"high"}, camera_id)
execute(analysis)
alert = Job.make("alert_write", str(uuid4()), {
    "camera_id":camera_id, "camera_fingerprint":camera_fingerprint, "directory_id":None,
    "directory_cooldown":False, "bypass_cooldown":False, "cooldown_seconds":60,
    "mode":"phone_use", "analysis_job_id":analysis.id, "alert_name":"test", "reason":"test",
    "confidence":.9, "event_phase":None, "event_started_at":None, "event_ended_at":None,
    "evidences":[{"camera_id":camera_id,"camera_name":"test","evidence_path":"test.jpg"}],
}, camera_id)
result = execute(alert)
assert result["alert_id"]
with session_scope() as session:
    assert session.scalar(select(func.count()).select_from(models.Alert).where(models.Alert.camera_id==camera_id)) == 1
    assert session.scalar(select(func.count()).select_from(models.BackgroundTask).where(
        models.BackgroundTask.kind=="notification", models.BackgroundTask.camera_id==camera_id)) == 1

rollback = Job.make("rollback_test", str(uuid4()), {}, camera_id)
store.persist(rollback)
claim = store.claim(["rollback_test"])
def failure(session, payload):
    row = session.get(models.Camera, camera_id)
    row.name = "must rollback"
    session.flush()
    raise RuntimeError("injected")
try:
    store.complete(rollback.id, claim["lease_token"], failure)
    raise AssertionError("Expected failure")
except RuntimeError:
    pass
with session_scope() as session:
    assert session.get(models.Camera, camera_id).name == "test"
    session.get(models.BackgroundTask, rollback.id).lease_until = utc_now() - timedelta(seconds=1)
restarted = TaskStore()
new_claim = restarted.claim(["rollback_test"])
assert new_claim["lease_token"] != claim["lease_token"]
assert not restarted.complete(rollback.id, claim["lease_token"], lambda *_: {})
assert restarted.complete(rollback.id, new_claim["lease_token"], lambda *_: {})
jobs = [Job.make("batch_test", str(uuid4()), {"value":i}) for i in range(10)]
store.persist_many(jobs)
claims = store.claim_many(["batch_test"])
assert len(claims) == 10
assert store.complete_many(claims, lambda s,k,p: p) == 10
assert store.complete_many(claims, lambda s,k,p: p) == 0
print(json.dumps({"postgres_transaction_checks":"passed", "checks":[
    "traffic_idempotency", "alert_dedup", "notification_same_transaction", "rollback", "restart_fencing", "batch_idempotency"
]}))
