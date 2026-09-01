from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch

from backend.pipeline import PHONE_USE_INTERVAL_SECONDS, MonitoringRuntime, yolo_required_modes
from backend.schemas import CameraOptions, Mode, VLMResult
from backend.vlm import VLMResponse


def test_phone_use_alone_does_not_require_yolo():
    assert yolo_required_modes({Mode.PHONE_USE.value}) == set()


def test_other_local_modes_still_require_yolo_with_phone_use():
    modes = {Mode.PHONE_USE.value, Mode.OFF_DUTY.value, Mode.INTRUSION.value}
    assert yolo_required_modes(modes) == {Mode.OFF_DUTY.value, Mode.INTRUSION.value}


def test_phone_use_interval_is_three_minutes():
    assert PHONE_USE_INTERVAL_SECONDS == 180


def test_phone_use_throttle_blocks_until_three_minutes_have_elapsed():
    runtime = object.__new__(MonitoringRuntime)
    runtime.last_mode_run = defaultdict(float)

    with patch("backend.pipeline.time.monotonic", side_effect=[1000.0, 1179.0, 1180.0]):
        assert runtime._mode_due("camera-1", Mode.PHONE_USE.value, PHONE_USE_INTERVAL_SECONDS, False)
        assert not runtime._mode_due("camera-1", Mode.PHONE_USE.value, PHONE_USE_INTERVAL_SECONDS, False)
        assert runtime._mode_due("camera-1", Mode.PHONE_USE.value, PHONE_USE_INTERVAL_SECONDS, False)


def test_phone_use_sends_one_frame_and_alerts_on_single_enhanced_confirmation():
    frame = b"current-frame"
    sampled = [SimpleNamespace(jpeg=b"old-frame-1"), SimpleNamespace(jpeg=b"old-frame-2")]
    response = VLMResponse(
        result=VLMResult(
            mode=Mode.PHONE_USE,
            status="confirmed",
            confidence=0.96,
            reason="明确看到人员操作手机",
        ),
        request_id="request-1",
        usage={},
        latency_ms=10,
        provider="test",
        model="enhanced-model",
    )
    received_frames = []
    alerts = []

    class VLM:
        async def tiered_analyze(self, mode, frames):
            received_frames.extend(frames)
            return response

    class Repository:
        def add_analysis(self, **kwargs):
            return SimpleNamespace(**kwargs)

    class Alerts:
        async def create(self, camera, analysis, evidence):
            alerts.append((camera, analysis, evidence))

    class State:
        def behavior_confirmed(self, *args):
            raise AssertionError("玩手机单帧确认不应使用连续窗口")

    runtime = object.__new__(MonitoringRuntime)
    runtime.vlm = VLM()
    runtime.repository = Repository()
    runtime.alerts = Alerts()
    runtime.media = SimpleNamespace(sample=lambda camera_id, count: sampled)
    camera = SimpleNamespace(id="camera-1")

    result = __import__("asyncio").run(
        runtime._behavior(
            camera,
            Mode.PHONE_USE,
            State(),
            CameraOptions(),
            frame,
            current_frame_only=True,
        )
    )

    assert received_frames == [frame]
    assert result["confirmed_window"] is True
    assert len(alerts) == 1
    assert alerts[0][2] == frame
