import pytest

from backend.detectors.fire_smoke import FireSmokeDetector
from backend.queueing import AnalysisQueue
from backend.schemas import WebhookSettingsUpdate


@pytest.mark.asyncio
async def test_fallback_queue_respects_priority_and_reports_per_priority_depth():
    queue = AnalysisQueue("redis://127.0.0.1:1/0")
    await queue.enqueue("flow-camera", "low")
    await queue.enqueue("intrusion-camera", "high")
    await queue.enqueue("fire-camera", "critical")
    assert await queue.depths() == {"critical": 1, "high": 1, "normal": 0, "low": 1}
    first = await queue.get()
    assert first.camera_id == "fire-camera"
    await queue.ack(first)


def test_fire_detector_rejects_hash_mismatch(tmp_path):
    model = tmp_path / "fire.pt"
    model.write_bytes(b"not-the-reviewed-model")
    detector = FireSmokeDetector(str(model), expected_sha256="0" * 64)
    assert not detector.available
    assert "SHA256 mismatch" in detector.detail


def test_existing_webhook_secret_can_be_kept_blank_on_update():
    payload = WebhookSettingsUpdate(enabled=True, url="https://example.com/events", secret="")
    assert payload.enabled
