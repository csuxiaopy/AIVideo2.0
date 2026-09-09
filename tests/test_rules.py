from datetime import datetime, timedelta, timezone

from backend.rules import (
    CameraRuleState,
    box_intersects_polygon,
    is_scheduled,
    point_in_polygon,
)
from backend.schemas import ScheduleSpec


def test_point_in_polygon():
    square = [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]
    assert point_in_polygon((0.5, 0.5), square)
    assert not point_in_polygon((0.95, 0.5), square)


def test_person_box_any_overlap_counts_as_inside_post_roi():
    roi = [(0.3, 0.3), (0.7, 0.3), (0.7, 0.7), (0.3, 0.7)]
    assert box_intersects_polygon((0.1, 0.1, 0.4, 0.4), roi)  # box corner enters ROI
    assert box_intersects_polygon((0.4, 0.4, 0.6, 0.6), roi)  # box fully inside ROI
    assert box_intersects_polygon((0.2, 0.45, 0.8, 0.55), roi)  # edges cross
    assert box_intersects_polygon((0.1, 0.1, 0.9, 0.9), roi)  # box contains ROI
    assert box_intersects_polygon((0.1, 0.3, 0.3, 0.6), roi)  # boundary touch
    assert not box_intersects_polygon((0.0, 0.0, 0.2, 0.2), roi)
    assert not box_intersects_polygon((0.7, 0.7, 0.6, 0.8), roi)
    assert not box_intersects_polygon((0.1, 0.1, 0.4, 0.4), [])


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


def _flow_step(state, now, offset, tracks, **kwargs):
    return state.flow_update(tracks, now + timedelta(seconds=offset), **kwargs)


def test_flow_counts_immediately_when_track_first_enters_roi():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    roi = [(0.4, 0.2), (0.8, 0.2), (0.8, 0.8), (0.4, 0.8)]
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 0, "roi": roi}
    assert _flow_step(state, now, 0, [(1, (0.2, 0.5))], **kwargs)[0] == 0
    entered, _, events = _flow_step(state, now, 1, [(1, (0.5, 0.5))], **kwargs)
    assert entered == 1
    assert [event.kind for event in events] == ["ENTERED"]


def test_first_seen_inside_counts_once_and_reentry_does_not_repeat():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    roi = [(0.4, 0.2), (0.8, 0.2), (0.8, 0.8), (0.4, 0.8)]
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 0, "roi": roi}
    entered, _, events = _flow_step(state, now, 0, [(1, (0.5, 0.5))], **kwargs)
    assert entered == 1
    assert [event.kind for event in events] == ["NEW", "ENTERED"]
    assert _flow_step(state, now, 1, [(1, (0.5, 0.5))], **kwargs)[0] == 0
    assert _flow_step(state, now, 2, [(1, (0.2, 0.5))], **kwargs)[0] == 0
    assert _flow_step(state, now, 3, [(1, (0.5, 0.5))], **kwargs)[0] == 0


def test_one_frame_track_inside_roi_counts_immediately():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    assert state.flow_update([(2, (0.5, 0.5))], now, recovery_grace_seconds=0)[0] == 1
    assert state.flow_update([], now + timedelta(seconds=1), recovery_grace_seconds=0)[0] == 0


def test_absence_event_emits_threshold_then_resolution_once():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    assert state.absence_event_update(False, True, 600, now) == (None, now)
    phase, started = state.absence_event_update(False, True, 600, now + timedelta(seconds=600))
    assert (phase, started) == ("threshold", now)
    assert state.absence_event_update(False, True, 600, now + timedelta(seconds=700))[0] is None
    assert state.absence_event_update(True, True, 600, now + timedelta(seconds=800)) == ("resolved", now)


def test_same_id_can_reassociate_before_three_missing_cycles():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 0}
    _flow_step(state, now, 0, [(4, (0.02, 0.5))], **kwargs)
    _flow_step(state, now, 1, [(4, (0.02, 0.5))], **kwargs)
    _flow_step(state, now, 2, [], **kwargs)
    _flow_step(state, now, 3, [], **kwargs)
    _, tracks, events = _flow_step(state, now, 4, [(4, (0.03, 0.5))], **kwargs)
    assert 4 in tracks
    assert [(event.kind, event.missing_cycles) for event in events] == [("REASSOCIATED", 2)]


def test_third_missing_cycle_loses_track_and_new_id_counts_independently():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 0}
    _flow_step(state, now, 0, [(4, (0.02, 0.5))], **kwargs)
    _flow_step(state, now, 1, [(4, (0.02, 0.5))], **kwargs)
    _flow_step(state, now, 2, [], **kwargs)
    _flow_step(state, now, 3, [], **kwargs)
    _, tracks, events = _flow_step(state, now, 4, [], **kwargs)
    assert 4 not in tracks
    assert [(event.kind, event.missing_cycles) for event in events] == [("LOST", 3)]
    entered, tracks, events = _flow_step(state, now, 5, [(9, (0.5, 0.5))], **kwargs)
    assert entered == 1 and 9 in tracks
    assert [event.kind for event in events] == ["NEW", "ENTERED"]


def test_startup_and_recovery_tracks_inside_roi_count_immediately():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 15}
    assert state.flow_update([(1, (0.5, 0.5))], now, **kwargs)[0] == 1
    assert state.flow_update([(1, (0.5, 0.5))], now + timedelta(seconds=1), **kwargs)[0] == 0
    assert state.flow_update([(2, (0.5, 0.5))], now + timedelta(seconds=20), recovering=True, **kwargs)[0] == 1
    assert state.flow_update([(2, (0.5, 0.5))], now + timedelta(seconds=21), **kwargs)[0] == 0


def test_two_people_entering_together_count_independently():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 0}
    roi = [(0.4, 0.1), (0.6, 0.1), (0.6, 0.9), (0.4, 0.9)]
    kwargs["roi"] = roi
    outside = [(10, (0.2, 0.3)), (11, (0.8, 0.7))]
    inside = [(10, (0.5, 0.3)), (11, (0.5, 0.7))]
    state.flow_update(outside, now, **kwargs)
    assert state.flow_update(inside, now + timedelta(seconds=1), **kwargs)[0] == 2
    assert state.flow_update(inside, now + timedelta(seconds=2), **kwargs)[0] == 0


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
