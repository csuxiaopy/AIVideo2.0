import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from backend.pipeline import OFF_DUTY_REVIEW_INTERVAL_SECONDS, MonitoringRuntime
from backend.rules import RuleStateRegistry
from backend.schemas import Mode, VLMResult
from backend.vlm import VLMError, VLMResponse, VisionModelClient


def _response(status: str) -> VLMResponse:
    return VLMResponse(
        results={Mode.OFF_DUTY: VLMResult(
            mode=Mode.OFF_DUTY, status=status, confidence=0.97, reason=f"review-{status}"
        )},
        request_id="review-request", usage={"total_tokens": 12}, latency_ms=23,
        provider="test", model="economy-model",
    )


def _runtime(vlm):
    analyses, alerts = [], []

    class Repository:
        def add_analysis(self, **kwargs):
            analyses.append(kwargs)
            return SimpleNamespace(**kwargs)

    class Alerts:
        async def create(self, camera, analysis, evidence, **kwargs):
            alerts.append((camera, analysis, evidence, kwargs))

    runtime = object.__new__(MonitoringRuntime)
    runtime.vlm = vlm
    runtime.repository = Repository()
    runtime.alerts = Alerts()
    runtime.rules = RuleStateRegistry()
    runtime.yolo = SimpleNamespace(model_name="local-yolo")
    return runtime, analyses, alerts


def _threshold(runtime, camera_id: str, now: datetime) -> None:
    state = runtime.rules.for_camera(camera_id)
    state.absence_event_update(False, True, 60, now - timedelta(seconds=60))
    state.absence_event_update(False, True, 60, now)


def test_pipeline_only_reviews_after_local_absence_threshold():
    assert OFF_DUTY_REVIEW_INTERVAL_SECONDS == 1800
    calls = []

    class VLM:
        async def analyze_off_duty(self, jpeg):
            calls.append(jpeg)
            return _response("confirmed")

    runtime, _, alerts = _runtime(VLM())
    runtime.last_mode_run = defaultdict(float)
    runtime.settings = SimpleNamespace(yolo_inference_timeout_seconds=5)
    ok, encoded = cv2.imencode(".jpg", np.zeros((80, 120, 3), dtype=np.uint8))
    assert ok

    class Media:
        def latest(self, camera_id):
            return SimpleNamespace(jpeg=encoded.tobytes())

        def set_person_detections(self, *args):
            pass

        def set_object_detections(self, *args):
            pass

        def set_intrusion(self, *args):
            pass

    runtime.media = Media()
    runtime.yolo = SimpleNamespace(
        detect=lambda camera_id, jpeg: [], people=lambda detections: [], model_name="local-yolo"
    )
    camera = SimpleNamespace(
        id="camera-1", name="一号工位", modes_json='["off_duty"]',
        options_json='{"off_duty_seconds":60,"shift_grace_seconds":0}',
        geometry_json='{"post_roi":[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]}',
        schedule_json="{}", intrusion_schedule_json="{}",
    )
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with patch("backend.pipeline.utc_now", return_value=started):
        asyncio.run(runtime._process(camera, force=True))
    with patch("backend.pipeline.utc_now", return_value=started + timedelta(seconds=59)):
        asyncio.run(runtime._process(camera, force=True))
    assert calls == []
    assert alerts == []

    with patch("backend.pipeline.utc_now", return_value=started + timedelta(seconds=60)):
        asyncio.run(runtime._process(camera, force=True))
    assert len(calls) == 1
    assert len(alerts) == 1


@pytest.mark.parametrize(("behavior_interval", "piggyback_expected"), [(60, True), (120, False), (180, False)])
def test_pipeline_routes_off_duty_review_by_active_threshold(behavior_interval, piggyback_expected):
    behavior_calls, dedicated_calls, alerts = [], [], []

    class VLM:
        async def analyze_behaviors(self, modes, jpeg):
            behavior_calls.append(set(modes))
            return VLMResponse(
                results={mode: VLMResult(
                    mode=mode, status="confirmed", confidence=0.97, reason="confirmed"
                ) for mode in modes},
                request_id="behavior", usage={}, latency_ms=1, provider="test", model="test",
            )

        async def analyze_off_duty(self, jpeg):
            dedicated_calls.append(jpeg)
            return _response("confirmed")

    class Repository:
        def add_analysis(self, **kwargs):
            return SimpleNamespace(**kwargs)

    class Alerts:
        async def create(self, camera, analysis, evidence, **kwargs):
            alerts.append((analysis.mode, evidence))

    runtime = object.__new__(MonitoringRuntime)
    runtime.vlm, runtime.repository, runtime.alerts = VLM(), Repository(), Alerts()
    runtime.rules = RuleStateRegistry()
    runtime.last_mode_run = defaultdict(float)
    runtime.settings = SimpleNamespace(yolo_inference_timeout_seconds=5)
    ok, encoded = cv2.imencode(".jpg", np.zeros((80, 120, 3), dtype=np.uint8))
    assert ok

    class Media:
        def latest(self, _camera_id):
            return SimpleNamespace(jpeg=encoded.tobytes())

        def set_person_detections(self, *_args):
            pass

        def set_object_detections(self, *_args):
            pass

        def set_intrusion(self, *_args):
            pass

    runtime.media = Media()
    runtime.yolo = SimpleNamespace(
        detect=lambda _camera_id, _jpeg: [], people=lambda _detections: [], model_name="local-yolo"
    )
    camera = SimpleNamespace(
        id="camera-1", name="一号工位", modes_json='["off_duty","phone_use"]',
        options_json=(
            f'{{"off_duty_seconds":120,"behavior_interval_seconds":{behavior_interval},'
            '"phone_use_seconds":600}'
        ),
        geometry_json='{"post_roi":[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]}',
        schedule_json="{}", intrusion_schedule_json="{}",
    )
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with patch("backend.pipeline.utc_now", return_value=started):
        asyncio.run(runtime._process(camera, force=True))
    with patch("backend.pipeline.utc_now", return_value=started + timedelta(seconds=120)):
        asyncio.run(runtime._process(camera, force=True))

    if piggyback_expected:
        assert all(Mode.OFF_DUTY in modes for modes in behavior_calls)
        assert dedicated_calls == []
    else:
        assert all(Mode.OFF_DUTY not in modes for modes in behavior_calls)
        assert len(dedicated_calls) == 1
    assert [mode for mode, _ in alerts].count(Mode.OFF_DUTY.value) == 1


def test_off_duty_confirmed_review_creates_one_alert_with_same_evidence():
    received = []

    class VLM:
        async def analyze_off_duty(self, jpeg):
            received.append(jpeg)
            return _response("confirmed")

    runtime, analyses, alerts = _runtime(VLM())
    camera = SimpleNamespace(id="camera-1", name="一号工位")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _threshold(runtime, camera.id, now)

    result = asyncio.run(runtime._review_off_duty(camera, b"annotated-frame", now - timedelta(seconds=60), now))

    assert result["status"] == "confirmed"
    assert received == [b"annotated-frame"]
    assert analyses[0]["request_id"] == "review-request"
    assert analyses[0]["local_model"] == "local-yolo"
    assert len(alerts) == 1
    assert alerts[0][2] == b"annotated-frame"
    assert alerts[0][3]["event_phase"] == "threshold"
    state = runtime.rules.for_camera(camera.id)
    assert state.absence_vlm_confirmed
    assert not state.off_duty_review_due(now + timedelta(hours=1), OFF_DUTY_REVIEW_INTERVAL_SECONDS)


@pytest.mark.parametrize("status", ["none", "suspected", "uncertain"])
def test_off_duty_non_confirmed_review_is_fail_closed(status):
    class VLM:
        async def analyze_off_duty(self, jpeg):
            return _response(status)

    runtime, analyses, alerts = _runtime(VLM())
    camera = SimpleNamespace(id="camera-1", name="一号工位")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _threshold(runtime, camera.id, now)

    result = asyncio.run(runtime._review_off_duty(camera, b"frame", now - timedelta(seconds=60), now))

    assert result["status"] == status
    assert analyses[0]["status"] == status
    assert alerts == []
    state = runtime.rules.for_camera(camera.id)
    assert not state.off_duty_review_due(
        now + timedelta(seconds=OFF_DUTY_REVIEW_INTERVAL_SECONDS - 1),
        OFF_DUTY_REVIEW_INTERVAL_SECONDS,
    )
    assert state.off_duty_review_due(
        now + timedelta(seconds=OFF_DUTY_REVIEW_INTERVAL_SECONDS),
        OFF_DUTY_REVIEW_INTERVAL_SECONDS,
    )


def test_off_duty_missing_model_and_error_are_logged_without_alert():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    camera = SimpleNamespace(id="camera-1", name="一号工位")
    runtime, analyses, alerts = _runtime(None)
    _threshold(runtime, camera.id, now)
    result = asyncio.run(runtime._review_off_duty(camera, b"frame", now - timedelta(seconds=60), now))
    assert result["status"] == "uncertain"
    assert analyses[0]["error"] == "model_not_configured"
    assert alerts == []

    class BrokenVLM:
        async def analyze_off_duty(self, jpeg):
            raise VLMError("bad json", "failed-request")

    runtime, analyses, alerts = _runtime(BrokenVLM())
    _threshold(runtime, camera.id, now)
    result = asyncio.run(runtime._review_off_duty(camera, b"frame", now - timedelta(seconds=60), now))
    assert result["status"] == "uncertain"
    assert analyses[0]["request_id"] == "failed-request"
    assert "bad json" in analyses[0]["error"]
    assert alerts == []


def test_off_duty_client_uses_dedicated_prompt_and_does_not_log_image():
    logs, bodies = [], []
    client = VisionModelClient("https://example.invalid/v1", "secret", "economy", log_writer=lambda **v: logs.append(v))

    class HTTP:
        async def post(self, url, headers, json):
            bodies.append(json)
            payload = {"id": "req-1", "choices": [{"message": {"content": (
                '{"result":{"mode":"off_duty","status":"confirmed",'
                '"confidence":0.99,"reason":"岗位区域清晰无人"}}'
            )}}], "usage": {"total_tokens": 8}}
            return SimpleNamespace(
                status_code=200, headers={}, text="model-response", is_error=False,
                json=lambda: payload,
            )

        async def aclose(self):
            pass

    asyncio.run(client.client.aclose())
    client.client = HTTP()
    response = asyncio.run(client.analyze_off_duty(b"candidate-image", "camera-1", "一号工位"))

    assert response.results[Mode.OFF_DUTY].status == "confirmed"
    assert "持续时间" in bodies[0]["messages"][0]["content"]
    content = bodies[0]["messages"][1]["content"]
    assert [item["type"] for item in content] == ["text", "image_url"]
    assert logs[0]["stage"] == "off_duty_final_review"
    assert logs[0]["modes_json"] == '["off_duty"]'
    assert not any("image" in key for key in logs[0])
