from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.schemas import GeometrySpec, ScheduleSpec

logger = logging.getLogger(__name__)


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersects = (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        if intersects:
            inside = not inside
        j = i
    return inside


def box_intersects_polygon(
    box: tuple[float, float, float, float], polygon: list[tuple[float, float]]
) -> bool:
    """Return true when any part of an axis-aligned detection box touches the ROI."""
    if len(polygon) < 3:
        return False
    x1, y1, x2, y2 = box
    if x2 < x1 or y2 < y1:
        return False

    def on_segment(
        point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
    ) -> bool:
        px, py = point
        ax, ay = start
        bx, by = end
        cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
        return (
            abs(cross) <= 1e-9
            and min(ax, bx) - 1e-9 <= px <= max(ax, bx) + 1e-9
            and min(ay, by) - 1e-9 <= py <= max(ay, by) + 1e-9
        )

    def orientation(
        a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
    ) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def segments_intersect(
        a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]
    ) -> bool:
        ab_c, ab_d = orientation(a, b, c), orientation(a, b, d)
        cd_a, cd_b = orientation(c, d, a), orientation(c, d, b)
        if ((ab_c > 0 > ab_d) or (ab_d > 0 > ab_c)) and (
            (cd_a > 0 > cd_b) or (cd_b > 0 > cd_a)
        ):
            return True
        return (
            (abs(ab_c) <= 1e-9 and on_segment(c, a, b))
            or (abs(ab_d) <= 1e-9 and on_segment(d, a, b))
            or (abs(cd_a) <= 1e-9 and on_segment(a, c, d))
            or (abs(cd_b) <= 1e-9 and on_segment(b, c, d))
        )

    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    if any(point_in_polygon(corner, polygon) for corner in corners):
        return True
    if any(x1 <= x <= x2 and y1 <= y <= y2 for x, y in polygon):
        return True

    box_edges = list(zip(corners, corners[1:] + corners[:1]))
    polygon_edges = list(zip(polygon, polygon[1:] + polygon[:1]))
    return any(
        segments_intersect(box_start, box_end, roi_start, roi_end)
        for box_start, box_end in box_edges
        for roi_start, roi_end in polygon_edges
    )


def is_scheduled(schedule: ScheduleSpec, now: datetime | None = None) -> bool:
    if not schedule.weekly:
        return True
    zone = ZoneInfo(schedule.timezone)
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    if current.date().isoformat() in schedule.holidays:
        return False
    current_minutes = current.hour * 60 + current.minute
    weekday = current.weekday()
    for shift in schedule.weekly.get(str(weekday), []):
        start_h, start_m = map(int, shift.start.split(":"))
        end_h, end_m = map(int, shift.end.split(":"))
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start <= end and start <= current_minutes < end:
            return True
        if start > end and current_minutes >= start:
            return True
    previous = (weekday - 1) % 7
    for shift in schedule.weekly.get(str(previous), []):
        start_h, start_m = map(int, shift.start.split(":"))
        end_h, end_m = map(int, shift.end.split(":"))
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start > end and current_minutes < end:
            return True
    return False


@dataclass
class FlowTrackState:
    track_id: int
    first_seen: datetime
    last_seen: datetime
    position: tuple[float, float]
    stable_zone: str | None = None
    candidate_zone: str | None = None
    stable_frames: int = 1
    outside_armed: bool = False
    suppressed: bool = False
    inside_roi: bool = False
    missing_cycles: int = 0
    trajectory: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=8))


@dataclass(frozen=True)
class FlowDebugEvent:
    track_id: int
    kind: str
    position: tuple[float, float]
    missing_cycles: int = 0


@dataclass
class CameraRuleState:
    black_consecutive: int = 0
    absence_since: datetime | None = None
    absence_alerted: bool = False
    phone_since: datetime | None = None
    phone_alerted: bool = False
    pending_resolutions: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)
    positive_windows: dict[str, deque[datetime]] = field(default_factory=lambda: defaultdict(deque))
    flow_tracks: dict[int, FlowTrackState] = field(default_factory=dict)
    flow_day: str | None = None
    flow_initialized_at: datetime | None = None
    flow_protection_until: datetime | None = None
    fire_consecutive: int = 0
    smoke_window: deque[bool] = field(default_factory=lambda: deque(maxlen=5))
    intrusion_active: set[int] = field(default_factory=set)
    intrusion_last_alert: dict[int, datetime] = field(default_factory=dict)
    intrusion_last_seen: dict[int, datetime] = field(default_factory=dict)
    shift_started_at: datetime | None = None
    was_scheduled: bool = False

    def black_update(self, is_black: bool) -> bool:
        self.black_consecutive = self.black_consecutive + 1 if is_black else 0
        return self.black_consecutive >= 3

    def absence_update(
        self,
        occupied: bool,
        scheduled: bool,
        threshold_seconds: int,
        now: datetime,
        grace_seconds: int = 0,
    ) -> bool:
        if scheduled and not self.was_scheduled:
            self.shift_started_at = now
        self.was_scheduled = scheduled
        if not scheduled or occupied:
            self.absence_since = None
            return False
        if self.shift_started_at and (now - self.shift_started_at).total_seconds() < grace_seconds:
            self.absence_since = None
            return False
        if self.absence_since is None:
            self.absence_since = now
            return False
        return (now - self.absence_since).total_seconds() >= threshold_seconds

    def absence_event_update(
        self, occupied: bool, scheduled: bool, threshold_seconds: int, now: datetime,
        grace_seconds: int = 0,
    ) -> tuple[str | None, datetime | None]:
        """Return threshold/resolved transitions and their event start time."""
        previous_start, previous_alerted = self.absence_since, self.absence_alerted
        reached = self.absence_update(occupied, scheduled, threshold_seconds, now, grace_seconds)
        if previous_alerted and self.absence_since is None:
            self.absence_alerted = False
            return "resolved", previous_start
        if reached and not self.absence_alerted:
            self.absence_alerted = True
            return "threshold", self.absence_since
        return None, self.absence_since

    def phone_event_update(
        self, confirmed: bool, threshold_seconds: int, now: datetime,
    ) -> tuple[str | None, datetime | None]:
        previous_start = self.phone_since
        if not confirmed:
            if self.phone_alerted:
                self.phone_since = None
                self.phone_alerted = False
                return "resolved", previous_start
            self.phone_since = None
            return None, None
        if self.phone_since is None:
            self.phone_since = now
            return None, self.phone_since
        if not self.phone_alerted and (now - self.phone_since).total_seconds() >= threshold_seconds:
            self.phone_alerted = True
            return "threshold", self.phone_since
        return None, self.phone_since

    def behavior_confirmed(self, mode: str, confirmed: bool, now: datetime) -> bool:
        values = self.positive_windows[mode]
        cutoff = now - timedelta(seconds=60)
        while values and values[0] < cutoff:
            values.popleft()
        if confirmed:
            values.append(now)
        return len(values) >= 2

    def fire_smoke_update(self, fire_hit: bool, smoke_hit: bool) -> tuple[bool, bool]:
        self.fire_consecutive = self.fire_consecutive + 1 if fire_hit else 0
        self.smoke_window.append(smoke_hit)
        return self.fire_consecutive >= 2, len(self.smoke_window) == 5 and sum(self.smoke_window) >= 3

    def intrusion_update(
        self,
        tracks: list[tuple[int, tuple[float, float]]],
        polygon: list[tuple[float, float]],
        now: datetime,
        cooldown_seconds: int,
    ) -> list[int]:
        current = {track_id for track_id, point in tracks if point_in_polygon(point, polygon)}
        for track_id in current:
            self.intrusion_last_seen[track_id] = now
        for track_id in list(self.intrusion_active - current):
            last_seen = self.intrusion_last_seen.get(track_id, now)
            if (now - last_seen).total_seconds() >= 1:
                self.intrusion_active.discard(track_id)
        triggered: list[int] = []
        for track_id in current - self.intrusion_active:
            self.intrusion_last_alert[track_id] = now
            triggered.append(track_id)
        self.intrusion_active.update(current)
        expiry = now - timedelta(seconds=max(60, cooldown_seconds * 2))
        self.intrusion_last_alert = {
            track_id: when for track_id, when in self.intrusion_last_alert.items() if when >= expiry
        }
        self.intrusion_last_seen = {
            track_id: when for track_id, when in self.intrusion_last_seen.items() if when >= expiry
        }
        return triggered

    def flow_update(
        self,
        tracks: list[tuple[int, tuple[float, float]]],
        now: datetime,
        min_stable_frames: int = 3,
        recovery_grace_seconds: int = 15,
        recovering: bool = False,
        roi: list[tuple[float, float]] | None = None,
        lost_cycles: int = 3,
    ) -> tuple[int, dict[int, FlowTrackState], list[FlowDebugEvent]]:
        day = now.date().isoformat()
        if self.flow_day != day:
            self.flow_day = day
            self.flow_tracks.clear()
            self.flow_initialized_at = None
            self.flow_protection_until = None
        if self.flow_initialized_at is None or recovering:
            self.flow_initialized_at = now
            self.flow_protection_until = now + timedelta(seconds=recovery_grace_seconds)
            if recovering:
                self.flow_tracks.clear()

        entered = 0
        events: list[FlowDebugEvent] = []
        active_roi = roi or [(0, 0), (1, 0), (1, 1), (0, 1)]
        incoming = {track_id: position for track_id, position in tracks}

        for track_id in set(self.flow_tracks) - set(incoming):
            state = self.flow_tracks[track_id]
            state.missing_cycles += 1
            if state.missing_cycles >= lost_cycles:
                events.append(FlowDebugEvent(track_id, "LOST", state.position, state.missing_cycles))
                self.flow_tracks.pop(track_id)

        for track_id, position in tracks:
            current_inside = point_in_polygon(position, active_roi)
            current_zone = "INSIDE" if current_inside else "OUTSIDE"
            state = self.flow_tracks.get(track_id)
            if state is None:
                state = FlowTrackState(
                    track_id=track_id, first_seen=now, last_seen=now, position=position,
                    candidate_zone=current_zone, inside_roi=current_inside,
                )
                state.trajectory.append(position)
                self.flow_tracks[track_id] = state
                events.append(FlowDebugEvent(track_id, "NEW", position))
                continue

            if state.missing_cycles:
                events.append(FlowDebugEvent(track_id, "REASSOCIATED", position, state.missing_cycles))
            state.missing_cycles = 0
            state.last_seen = now
            state.position = position
            state.inside_roi = current_inside
            state.trajectory.append(position)

            if state.candidate_zone == current_zone:
                state.stable_frames += 1
            else:
                state.candidate_zone = current_zone
                state.stable_frames = 1

            if state.stable_frames < min_stable_frames or state.stable_zone == current_zone:
                continue

            previous_zone = state.stable_zone
            state.stable_zone = current_zone
            if current_zone == "OUTSIDE":
                state.outside_armed = True
            elif previous_zone == "OUTSIDE" and state.outside_armed and not state.suppressed:
                state.outside_armed = False
                entered += 1
                events.append(FlowDebugEvent(track_id, "ENTERED", position))
                logger.info("[FLOW] Track %s crossed OUTSIDE -> INSIDE; visitor_count +1", track_id)
        return entered, dict(self.flow_tracks), events


class RuleStateRegistry:
    def __init__(self):
        self._states: dict[str, CameraRuleState] = {}

    def for_camera(self, camera_id: str) -> CameraRuleState:
        return self._states.setdefault(camera_id, CameraRuleState())

    def remove(self, camera_id: str) -> None:
        self._states.pop(camera_id, None)
