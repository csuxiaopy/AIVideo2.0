import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.pipeline import BEHAVIOR_INTERVAL_SECONDS, MonitoringRuntime, yolo_required_modes
from backend.rules import RuleStateRegistry
from backend.schemas import CameraOptions, Mode, ScheduleSpec, VLMResult
from backend.vlm import VLMError, VLMResponse, VisionModelClient


def make_result(mode: Mode, status: str = "none") -> VLMResult:
    return VLMResult(mode=mode, status=status, confidence=0.96, reason=f"{mode.value}-{status}")


@pytest.mark.parametrize("modes", [
    {Mode.PHONE_USE.value},
    {Mode.SMOKING.value},
    {Mode.PHONE_USE.value, Mode.SMOKING.value},
])
def test_behavior_modes_do_not_require_yolo(modes):
    assert yolo_required_modes(modes) == set()


def test_other_local_modes_still_require_yolo_with_behaviors():
    modes = {Mode.PHONE_USE.value, Mode.SMOKING.value, Mode.OFF_DUTY.value, Mode.INTRUSION.value}
    assert yolo_required_modes(modes) == {Mode.OFF_DUTY.value, Mode.INTRUSION.value}


def test_behavior_interval_is_three_minutes_and_shared():
    assert BEHAVIOR_INTERVAL_SECONDS == 180
    runtime = object.__new__(MonitoringRuntime)
    runtime.last_mode_run = defaultdict(float)
    with patch("backend.pipeline.time.monotonic", side_effect=[1000.0, 1179.0, 1180.0]):
        assert runtime._mode_due("camera-1", "behavior", BEHAVIOR_INTERVAL_SECONDS, False)
        assert not runtime._mode_due("camera-1", "behavior", BEHAVIOR_INTERVAL_SECONDS, False)
        assert runtime._mode_due("camera-1", "behavior", BEHAVIOR_INTERVAL_SECONDS, False)


def test_combined_behaviors_split_records_and_alerts_using_same_frame():
    frame = b"current-frame"
    response = VLMResponse(
        results={
            Mode.PHONE_USE: make_result(Mode.PHONE_USE, "confirmed"),
            Mode.SMOKING: make_result(Mode.SMOKING, "confirmed"),
        },
        request_id="request-1", usage={"total_tokens": 10}, latency_ms=10,
        provider="test", model="economy-model",
    )
    received, analyses, alerts = [], [], []

    class VLM:
        async def analyze_behaviors(self, modes, jpeg):
            received.append((modes, jpeg))
            return response

    class Repository:
        def add_analysis(self, **kwargs):
            analyses.append(kwargs)
            return SimpleNamespace(**kwargs)

    class Alerts:
        async def create(self, camera, analysis, evidence, **kwargs):
            alerts.append((analysis.mode, evidence))

    runtime = object.__new__(MonitoringRuntime)
    runtime.vlm, runtime.repository, runtime.alerts = VLM(), Repository(), Alerts()
    modes = {Mode.PHONE_USE, Mode.SMOKING}
    output = asyncio.run(runtime._behaviors(SimpleNamespace(id="camera-1"), modes, frame))

    assert received == [(modes, frame)]
    assert {item["mode"] for item in output} == {"phone_use", "smoking"}
    assert {item["mode"] for item in analyses} == {"phone_use", "smoking"}
    assert alerts == [("smoking", frame)]


def test_phone_use_only_alerts_at_threshold():
    current = {"status": "confirmed"}
    alerts = []

    class VLM:
        async def analyze_behaviors(self, modes, jpeg):
            result = make_result(Mode.PHONE_USE, current["status"])
            return VLMResponse(results={Mode.PHONE_USE: result}, request_id="r", usage={},
                               latency_ms=1, provider="test", model="test")

    class Repository:
        def add_analysis(self, **kwargs):
            return SimpleNamespace(**kwargs)

    class Alerts:
        async def create(self, camera, analysis, evidence, **kwargs):
            alerts.append(kwargs)

    runtime = object.__new__(MonitoringRuntime)
    runtime.vlm, runtime.repository, runtime.alerts = VLM(), Repository(), Alerts()
    runtime.rules = RuleStateRegistry()
    camera = SimpleNamespace(id="camera-1")
    options = CameraOptions(phone_use_seconds=600)
    schedule = ScheduleSpec(timezone="UTC")
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    asyncio.run(runtime._behaviors(camera, {Mode.PHONE_USE}, b"frame", options, schedule, started))
    asyncio.run(runtime._behaviors(camera, {Mode.PHONE_USE}, b"frame", options, schedule, started + timedelta(seconds=600)))
    current["status"] = "none"
    asyncio.run(runtime._behaviors(camera, {Mode.PHONE_USE}, b"frame", options, schedule, started + timedelta(seconds=700)))
    assert [item["event_phase"] for item in alerts] == ["threshold"]
    state = runtime.rules.for_camera(camera.id)
    assert state.phone_since is None
    assert not state.phone_alerted


def test_phone_use_interruption_before_threshold_restarts_accumulation():
    current = {"status": "confirmed"}
    alerts = []

    class VLM:
        async def analyze_behaviors(self, modes, jpeg):
            result = make_result(Mode.PHONE_USE, current["status"])
            return VLMResponse(results={Mode.PHONE_USE: result}, request_id="r", usage={},
                               latency_ms=1, provider="test", model="test")

    class Repository:
        def add_analysis(self, **kwargs):
            return SimpleNamespace(**kwargs)

    class Alerts:
        async def create(self, camera, analysis, evidence, **kwargs):
            alerts.append(kwargs)

    runtime = object.__new__(MonitoringRuntime)
    runtime.vlm, runtime.repository, runtime.alerts = VLM(), Repository(), Alerts()
    runtime.rules = RuleStateRegistry()
    camera = SimpleNamespace(id="camera-1")
    options = CameraOptions(phone_use_seconds=600)
    schedule = ScheduleSpec(timezone="UTC")
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)

    asyncio.run(runtime._behaviors(camera, {Mode.PHONE_USE}, b"frame", options, schedule, started))
    current["status"] = "none"
    asyncio.run(runtime._behaviors(
        camera, {Mode.PHONE_USE}, b"frame", options, schedule, started + timedelta(seconds=300)
    ))
    current["status"] = "confirmed"
    restarted = started + timedelta(seconds=400)
    asyncio.run(runtime._behaviors(camera, {Mode.PHONE_USE}, b"frame", options, schedule, restarted))
    asyncio.run(runtime._behaviors(
        camera, {Mode.PHONE_USE}, b"frame", options, schedule, restarted + timedelta(seconds=599)
    ))

    assert alerts == []
    assert runtime.rules.for_camera(camera.id).phone_since == restarted


def test_phone_use_no_frame_resets_state_without_pending_alert():
    runtime = object.__new__(MonitoringRuntime)
    runtime.rules = RuleStateRegistry()
    runtime.media = SimpleNamespace(latest=lambda _camera_id: None)
    camera = SimpleNamespace(
        id="camera-1", modes_json='["phone_use"]',
        options_json='{"phone_use_seconds":600}',
    )
    state = runtime.rules.for_camera(camera.id)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state.phone_event_update(True, 600, started)
    state.phone_event_update(True, 600, started + timedelta(seconds=600))

    result = asyncio.run(runtime._process(camera))

    assert result["skipped"] == "no_frame"
    assert state.phone_since is None
    assert not state.phone_alerted
    assert not hasattr(state, "pending_resolutions")


def test_phone_use_inactive_schedule_resets_state_without_alert():
    class Media:
        def latest(self, _camera_id):
            return SimpleNamespace(jpeg=b"frame")

        def set_person_detections(self, *_args):
            pass

        def set_object_detections(self, *_args):
            pass

        def set_intrusion(self, *_args):
            pass

    runtime = object.__new__(MonitoringRuntime)
    runtime.rules = RuleStateRegistry()
    runtime.media = Media()
    runtime.yolo = SimpleNamespace(people=lambda _detections: [])
    camera = SimpleNamespace(
        id="camera-1", modes_json='["phone_use"]',
        options_json='{"phone_use_seconds":600}', geometry_json="{}",
        schedule_json=(
            '{"timezone":"UTC","weekly":'
            '{"0":[],"1":[],"2":[],"3":[],"4":[],"5":[],"6":[]}}'
        ),
        intrusion_schedule_json="{}",
    )
    state = runtime.rules.for_camera(camera.id)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state.phone_event_update(True, 600, started)
    state.phone_event_update(True, 600, started + timedelta(seconds=600))

    result = asyncio.run(runtime._process(camera))

    assert result["results"] == []
    assert state.phone_since is None
    assert not state.phone_alerted


def test_phone_use_missing_model_keeps_uncertain_analysis_without_alert():
    analyses, alerts = [], []

    class Repository:
        def add_analysis(self, **kwargs):
            analyses.append(kwargs)
            return SimpleNamespace(**kwargs)

    class Alerts:
        async def create(self, *args, **kwargs):
            alerts.append((args, kwargs))

    runtime = object.__new__(MonitoringRuntime)
    runtime.vlm, runtime.repository, runtime.alerts = None, Repository(), Alerts()
    runtime.rules = RuleStateRegistry()
    camera = SimpleNamespace(id="camera-1")
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = runtime.rules.for_camera(camera.id)
    state.phone_event_update(True, 600, started)
    state.phone_event_update(True, 600, started + timedelta(seconds=600))

    output = asyncio.run(runtime._behaviors(
        camera, {Mode.PHONE_USE}, b"frame", CameraOptions(phone_use_seconds=600),
        ScheduleSpec(timezone="UTC"), started + timedelta(seconds=700),
    ))

    assert output[0]["status"] == "uncertain"
    assert analyses[0]["status"] == "uncertain"
    assert analyses[0]["error"] == "model_not_configured"
    assert alerts == []
    assert state.phone_since is None
    assert not state.phone_alerted


def test_combined_behavior_error_records_uncertain_for_every_requested_mode():
    analyses, alerts = [], []

    class VLM:
        async def analyze_behaviors(self, modes, jpeg):
            raise VLMError("bad json", "request-2")

    class Repository:
        def add_analysis(self, **kwargs):
            analyses.append(kwargs)
            return SimpleNamespace(**kwargs)

    class Alerts:
        async def create(self, *args, **kwargs):
            alerts.append((args, kwargs))

    runtime = object.__new__(MonitoringRuntime)
    runtime.vlm, runtime.repository, runtime.alerts = VLM(), Repository(), Alerts()
    runtime.rules = RuleStateRegistry()
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = runtime.rules.for_camera("camera-1")
    state.phone_event_update(True, 600, started)
    state.phone_event_update(True, 600, started + timedelta(seconds=600))
    output = asyncio.run(runtime._behaviors(
        SimpleNamespace(id="camera-1"), {Mode.PHONE_USE, Mode.SMOKING}, b"frame",
        CameraOptions(phone_use_seconds=600), ScheduleSpec(timezone="UTC"),
        started + timedelta(seconds=700),
    ))

    assert [item["status"] for item in output] == ["uncertain", "uncertain"]
    assert {item["mode"] for item in analyses} == {"phone_use", "smoking"}
    assert alerts == []
    assert state.phone_since is None
    assert not state.phone_alerted


def test_behavior_analysis_uses_single_configured_model(monkeypatch):
    client = object.__new__(VisionModelClient)
    client.base_url = "https://model.test/v1"
    client.api_key = "secret"
    client.economy_model = "economy"
    client.log_writer = None
    client.client = SimpleNamespace(post=None)
    calls = []

    async def post(url, headers, json):
        calls.append(json)
        payload = {"choices": [{"message": {"content": (
            '{"results":[{"mode":"phone_use","status":"suspected","confidence":0.8},'
            '{"mode":"smoking","status":"none","confidence":0.9}]}'
        )}}]}
        return SimpleNamespace(status_code=200, headers={}, text=str(payload), is_error=False,
                               json=lambda: payload)

    client.client.post = post
    returned = asyncio.run(client.analyze_behaviors({Mode.PHONE_USE, Mode.SMOKING}, b"frame"))
    assert returned.model == "economy"
    assert len(calls) == 1
    assert calls[0]["model"] == "economy"
    system_prompt = calls[0]["messages"][0]["content"]
    assert "工作人员" in system_prompt
    assert "前方有客户" in system_prompt
    assert "操作鼠标" in system_prompt
    assert "工作场景证据" in system_prompt
    assert "无法区分工作用途和娱乐用途" in system_prompt
    requested_modes = calls[0]["messages"][1]["content"][0]["text"]
    assert "phone_use" in requested_modes
    assert "smoking" in requested_modes
