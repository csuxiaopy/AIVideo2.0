import pytest

from backend import models
from backend.database import Base, engine, session_scope
from backend.repository import Repository, as_json, from_json
from backend.schemas import WebhookManualSend, WebhookTargetCreate, WebhookTargetUpdate


def test_webhook_levels_are_exact_and_validated():
    payload = WebhookTargetCreate(
        name="critical only", enabled=True, url="https://example.com/hook",
        auto_severities=["critical"],
    )
    assert payload.auto_severities == ["critical"]
    with pytest.raises(ValueError):
        WebhookTargetCreate(name="bad", url="https://example.com/hook", auto_severities=["warning"])


def test_update_does_not_require_a_secret():
    payload = WebhookTargetUpdate(
        name="existing", enabled=True, url="https://example.com/hook",
        auto_severities=["normal", "high"],
    )
    assert payload.enabled


def test_manual_send_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        WebhookManualSend(alert_ids=[1, 1], webhook_target_ids=[2])


def test_target_crud_and_delivery_upsert():
    Base.metadata.create_all(engine)
    repository = Repository()
    camera_id = "test-webhook-camera"
    with session_scope() as session:
        session.add(models.Camera(id=camera_id, name="webhook test", rtsp_url_encrypted="encrypted"))
    alert = repository.add_alert(camera_id=camera_id, mode="intrusion", severity="high")
    target = repository.create_webhook_target({
        "name": "operations", "enabled": True, "url": "https://example.com/hook",
        "secret_encrypted": "encrypted", "auto_severities_json": as_json(["high"]),
    })
    assert from_json(target.auto_severities_json, []) == ["high"]
    first = repository.upsert_webhook_delivery(alert.id, target, "automatic")
    second = repository.upsert_webhook_delivery(alert.id, target, "manual")
    assert first.id == second.id
    assert repository.webhook_deliveries(alert.id)[0].trigger == "manual"
    assert repository.delete_webhook_target(target.id)
    assert repository.webhook_deliveries(alert.id)[0].target_name == "operations"
    with session_scope() as session:
        camera = session.get(models.Camera, camera_id)
        if camera:
            session.delete(camera)
