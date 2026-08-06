from datetime import datetime, timedelta, timezone

from backend.rules import CameraRuleState, is_scheduled, line_side, point_in_polygon
from backend.schemas import ScheduleSpec


def test_point_in_polygon():
    square = [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]
    assert point_in_polygon((0.5, 0.5), square)
    assert not point_in_polygon((0.95, 0.5), square)


def test_cross_midnight_schedule():
    schedule = ScheduleSpec.model_validate({
        "timezone": "UTC", "weekly": {"0": [{"start": "22:00", "end": "06:00"}]}
    })
    assert is_scheduled(schedule, datetime(2026, 8, 3, 23, 0, tzinfo=timezone.utc))
    assert is_scheduled(schedule, datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc))
    assert not is_scheduled(schedule, datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))


def test_black_and_behavior_windows():
    state = CameraRuleState()
    assert not state.black_update(True)
    assert not state.black_update(True)
    assert state.black_update(True)
    now = datetime.now(timezone.utc)
    assert not state.behavior_confirmed("smoking", True, now)
    assert state.behavior_confirmed("smoking", True, now)


def test_flow_crossing_deduplicates():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    line = [(0.5, 0.0), (0.5, 1.0)]
    assert state.flow_update([(1, (0.4, 0.5))], line, now) == (0, 0)
    assert state.flow_update([(1, (0.6, 0.5))], line, now) == (0, 1)
    assert state.flow_update([(1, (0.7, 0.5))], line, now) == (0, 0)
    assert state.flow_update([(1, (0.4, 0.5))], line, now) == (0, 0)


def test_fire_and_smoke_confirmation_windows():
    state = CameraRuleState()
    assert state.fire_smoke_update(True, True) == (False, False)
    assert state.fire_smoke_update(True, False) == (True, False)
    assert state.fire_smoke_update(False, True) == (False, False)
    assert state.fire_smoke_update(False, False) == (False, False)
    assert state.fire_smoke_update(False, True) == (False, True)


def test_intrusion_first_entry_stay_and_reentry():
    state = CameraRuleState()
    zone = [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]
    now = datetime.now(timezone.utc)
    assert state.intrusion_update([(7, (0.5, 0.8))], zone, now, 60) == [7]
    assert state.intrusion_update([(7, (0.6, 0.8))], zone, now + timedelta(seconds=1), 60) == []
    assert state.intrusion_update([], zone, now + timedelta(seconds=3), 60) == []
    assert state.intrusion_update([(7, (0.5, 0.8))], zone, now + timedelta(seconds=4), 60) == [7]


def test_shift_grace_precedes_absence_timer():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    assert not state.absence_update(False, True, 300, now, grace_seconds=60)
    assert not state.absence_update(False, True, 300, now + timedelta(seconds=59), grace_seconds=60)
    assert not state.absence_update(False, True, 300, now + timedelta(seconds=60), grace_seconds=60)
    assert state.absence_update(False, True, 300, now + timedelta(seconds=360), grace_seconds=60)
