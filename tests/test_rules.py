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


def test_new_edge_track_counts_after_stable_frames_once():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"recovery_grace_seconds": 0}
    assert state.flow_update([(1, (0.02, 0.5))], now, **kwargs)[0] == 0
    assert state.flow_update([(1, (0.05, 0.5))], now + timedelta(seconds=1), **kwargs)[0] == 0
    entered, tracks = state.flow_update([(1, (0.09, 0.5))], now + timedelta(seconds=2), **kwargs)
    assert entered == 1
    assert tracks[1].counted
    assert state.flow_update([(1, (0.5, 0.5))], now + timedelta(seconds=3), **kwargs)[0] == 0


def test_one_frame_false_detection_does_not_count():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    assert state.flow_update([(2, (0.02, 0.5))], now, recovery_grace_seconds=0)[0] == 0
    assert state.flow_update([], now + timedelta(seconds=1), recovery_grace_seconds=0)[0] == 0


def test_id_switch_inherits_counted_state():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 0}
    state.flow_update([(4, (0.02, 0.5))], now, **kwargs)
    assert state.flow_update([(4, (0.05, 0.5))], now + timedelta(seconds=1), **kwargs)[0] == 1
    state.flow_update([], now + timedelta(seconds=2), **kwargs)
    entered, tracks = state.flow_update([(9, (0.06, 0.5))], now + timedelta(seconds=3), **kwargs)
    assert entered == 0
    assert tracks[9].counted


def test_startup_and_recovery_tracks_are_suppressed():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 15}
    state.flow_update([(1, (0.02, 0.5))], now, **kwargs)
    assert state.flow_update([(1, (0.05, 0.5))], now + timedelta(seconds=1), **kwargs)[0] == 0
    state.flow_update([(2, (0.02, 0.5))], now + timedelta(seconds=20), recovering=True, **kwargs)
    assert state.flow_update([(2, (0.05, 0.5))], now + timedelta(seconds=21), **kwargs)[0] == 0


def test_center_track_requires_extra_stability_and_motion():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"min_stable_frames": 3, "recovery_grace_seconds": 0}
    for index, x in enumerate((0.5, 0.51, 0.52, 0.53)):
        assert state.flow_update([(8, (x, 0.5))], now + timedelta(seconds=index), **kwargs)[0] == 0
    assert state.flow_update([(8, (0.55, 0.5))], now + timedelta(seconds=4), **kwargs)[0] == 1


def test_two_people_entering_together_count_independently():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 0}
    state.flow_update([(10, (0.02, 0.3)), (11, (0.98, 0.7))], now, **kwargs)
    assert state.flow_update(
        [(10, (0.05, 0.3)), (11, (0.95, 0.7))], now + timedelta(seconds=1), **kwargs
    )[0] == 2


def test_person_reentering_after_reassociation_window_counts_again():
    state = CameraRuleState()
    now = datetime.now(timezone.utc)
    kwargs = {"min_stable_frames": 2, "recovery_grace_seconds": 0, "reassociation_seconds": 5}
    state.flow_update([(12, (0.02, 0.5))], now, **kwargs)
    assert state.flow_update([(12, (0.05, 0.5))], now + timedelta(seconds=1), **kwargs)[0] == 1
    state.flow_update([], now + timedelta(seconds=2), **kwargs)
    state.flow_update([(13, (0.02, 0.5))], now + timedelta(seconds=8), **kwargs)
    assert state.flow_update([(13, (0.05, 0.5))], now + timedelta(seconds=9), **kwargs)[0] == 1


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
