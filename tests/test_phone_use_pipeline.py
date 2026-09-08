import asyncio
from collections import defaultdict
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
        provider="test", model="enhanced-model",
    )
    received, analyses, alerts = [], [], []

    class VLM:
        async def tiered_analyze_behaviors(self, modes, jpeg):
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


def test_phone_use_alerts_at_threshold_and_resolution():
    current = {"status": "confirmed"}
    alerts = []

    class VLM:
        async def tiered_analyze_behaviors(self, modes, jpeg):
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
    from datetime import datetime, timedelta, timezone
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    asyncio.run(runtime._behaviors(camera, {Mode.PHONE_USE}, b"frame", options, schedule, started))
    asyncio.run(runtime._behaviors(camera, {Mode.PHONE_USE}, b"frame", options, schedule, started + timedelta(seconds=600)))
    current["status"] = "none"
    asyncio.run(runtime._behaviors(camera, {Mode.PHONE_USE}, b"frame", options, schedule, started + timedelta(seconds=700)))
    assert [item["event_phase"] for item in alerts] == ["threshold", "resolved"]


def test_combined_behavior_error_records_uncertain_for_every_requested_mode():
    analyses = []

    class VLM:
        async def tiered_analyze_behaviors(self, modes, jpeg):
            raise VLMError("bad json", "request-2")

    class Repository:
        def add_analysis(self, **kwargs):
            analyses.append(kwargs)
            return SimpleNamespace(**kwargs)

    runtime = object.__new__(MonitoringRuntime)
    runtime.vlm, runtime.repository, runtime.alerts = VLM(), Repository(), SimpleNamespace()
    output = asyncio.run(runtime._behaviors(
        SimpleNamespace(id="camera-1"), {Mode.PHONE_USE, Mode.SMOKING}, b"frame"
    ))

    assert [item["status"] for item in output] == ["uncertain", "uncertain"]
    assert {item["mode"] for item in analyses} == {"phone_use", "smoking"}


def test_tiered_behavior_analysis_skips_enhanced_when_all_none(monkeypatch):
    client = object.__new__(VisionModelClient)
    calls = []
    economy = VLMResponse(
        results={Mode.PHONE_USE: make_result(Mode.PHONE_USE), Mode.SMOKING: make_result(Mode.SMOKING)},
        request_id="economy", usage={}, latency_ms=1, provider="test", model="economy",
    )

    async def analyze(modes, frame, enhanced=False):
        calls.append(enhanced)
        return economy

    monkeypatch.setattr(client, "analyze_behaviors", analyze)
    returned = asyncio.run(client.tiered_analyze_behaviors({Mode.PHONE_USE, Mode.SMOKING}, b"frame"))
    assert returned is economy
    assert calls == [False]


def test_tiered_behavior_analysis_enhances_all_modes_on_any_non_none(monkeypatch):
    client = object.__new__(VisionModelClient)
    calls = []
    modes = {Mode.PHONE_USE, Mode.SMOKING}
    economy = VLMResponse(
        results={Mode.PHONE_USE: make_result(Mode.PHONE_USE, "suspected"), Mode.SMOKING: make_result(Mode.SMOKING)},
        request_id="economy", usage={}, latency_ms=1, provider="test", model="economy",
    )
    enhanced_response = VLMResponse(
        results={Mode.PHONE_USE: make_result(Mode.PHONE_USE), Mode.SMOKING: make_result(Mode.SMOKING)},
        request_id="enhanced", usage={}, latency_ms=1, provider="test", model="enhanced",
    )

    async def analyze(requested, frame, enhanced=False):
        calls.append((set(requested), enhanced))
        return enhanced_response if enhanced else economy

    monkeypatch.setattr(client, "analyze_behaviors", analyze)
    returned = asyncio.run(client.tiered_analyze_behaviors(modes, b"frame"))
    assert returned is enhanced_response
    assert calls == [(modes, False), (modes, True)]
