import pytest
from types import ModuleType

import backend.detectors.yolo as yolo_module
from backend.detectors.fire_smoke import FireSmokeDetector
from backend.detectors.yolo import YoloDetector
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


def test_yolo_missing_model_is_degraded_without_crashing(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "ultralytics", None)
    detector = YoloDetector("models/missing-yolo26s.pt", "cpu", 640, 0.35, 0.5)
    assert not detector.available
    assert detector.status()["status"] == "degraded"
    with pytest.raises(RuntimeError):
        detector.detect("camera-1", b"not-an-image")


def test_yolo_process_pool_configuration_and_main_process_tracking(monkeypatch):
    class ImmediateFuture:
        def result(self):
            return 100, 200, [(0, "person", 0.9, (0.1, 0.2, 0.3, 0.8))]

    class FakeExecutor:
        def __init__(self, max_workers, **kwargs):
            self.max_workers = max_workers
            self.closed = False

        def submit(self, *args):
            return ImmediateFuture()

        def shutdown(self, **kwargs):
            self.closed = True

    monkeypatch.setitem(__import__("sys").modules, "ultralytics", ModuleType("ultralytics"))
    monkeypatch.setitem(__import__("sys").modules, "supervision", None)
    monkeypatch.setattr(yolo_module, "ProcessPoolExecutor", FakeExecutor)
    detector = YoloDetector("models/yolo26s.pt", "cpu", 640, 0.35, 0.5, 4, 5, 1)
    detections = detector.detect("camera-1", b"jpeg")
    assert len(detections) == 1
    assert detections[0].track_id is not None
    assert detector.status()["inference_processes"] == 4
    assert detector.status()["threads_per_process"] == 5
    executor = detector.executor
    detector.close()
    assert executor.closed
