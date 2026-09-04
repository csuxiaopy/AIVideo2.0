from __future__ import annotations

import math
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
    first_position: tuple[float, float]
    first_zone: str
    stable_frames: int = 1
    counted: bool = False
    suppressed: bool = False
    trajectory: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=8))


def flow_entry_zone(point: tuple[float, float], ratio: float) -> str:
    x, y = point
    distances = {"LEFT_EDGE": x, "RIGHT_EDGE": 1 - x, "TOP_EDGE": y, "BOTTOM_EDGE": 1 - y}
    zone, distance = min(distances.items(), key=lambda item: item[1])
    return zone if distance <= ratio else "CENTER"


@dataclass
class CameraRuleState:
    black_consecutive: int = 0
    absence_since: datetime | None = None
    positive_windows: dict[str, deque[datetime]] = field(default_factory=lambda: defaultdict(deque))
    flow_tracks: dict[int, FlowTrackState] = field(default_factory=dict)
    recently_lost_flow_tracks: dict[int, FlowTrackState] = field(default_factory=dict)
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
        edge_ratio: float = 0.1,
        reassociation_seconds: int = 5,
        reassociation_distance: float = 0.12,
        recovery_grace_seconds: int = 15,
        recovering: bool = False,
    ) -> tuple[int, dict[int, FlowTrackState]]:
        day = now.date().isoformat()
        if self.flow_day != day:
            self.flow_day = day
            self.flow_tracks.clear()
            self.recently_lost_flow_tracks.clear()
            self.flow_initialized_at = None
            self.flow_protection_until = None
        if self.flow_initialized_at is None or recovering:
            self.flow_initialized_at = now
            self.flow_protection_until = now + timedelta(seconds=recovery_grace_seconds)
            if recovering:
                self.recently_lost_flow_tracks.update(self.flow_tracks)
                self.flow_tracks.clear()

        incoming = {track_id: position for track_id, position in tracks}
        for track_id in set(self.flow_tracks) - set(incoming):
            self.recently_lost_flow_tracks[track_id] = self.flow_tracks.pop(track_id)

        lost_cutoff = now - timedelta(seconds=reassociation_seconds)
        self.recently_lost_flow_tracks = {
            track_id: state for track_id, state in self.recently_lost_flow_tracks.items()
            if state.last_seen >= lost_cutoff
        }
        entered = 0
        protected = bool(self.flow_protection_until and now < self.flow_protection_until)
        for track_id, position in tracks:
            state = self.flow_tracks.get(track_id)
            if state is None:
                match = min(
                    (
                        (math.dist(position, old.trajectory[-1]), old_id, old)
                        for old_id, old in self.recently_lost_flow_tracks.items()
                        if old.trajectory and math.dist(position, old.trajectory[-1]) <= reassociation_distance
                    ),
                    default=None,
                )
                if match:
                    _, old_id, old = match
                    self.recently_lost_flow_tracks.pop(old_id, None)
                    state = FlowTrackState(
                        track_id, old.first_seen, now, old.first_position, old.first_zone,
                        old.stable_frames + 1, old.counted, old.suppressed,
                        deque(old.trajectory, maxlen=8),
                    )
                    logger.info("[FLOW] Track %s possibly reassociated with Track %s; inherit counted=%s", track_id, old_id, old.counted)
                else:
                    zone = flow_entry_zone(position, edge_ratio)
                    state = FlowTrackState(track_id, now, now, position, zone, suppressed=protected)
                    logger.info("[FLOW] Track %s created first_zone=%s%s", track_id, zone, " (protected)" if protected else "")
                    if zone == "CENTER":
                        logger.info("[FLOW] Track %s created at CENTER; possible tracker recreation", track_id)
                state.trajectory.append(position)
                self.flow_tracks[track_id] = state
            else:
                state.stable_frames += 1
                state.last_seen = now
                state.trajectory.append(position)

            displacement = math.dist(state.first_position, position)
            required = min_stable_frames if state.first_zone != "CENTER" else min_stable_frames + 2
            eligible_center = state.first_zone != "CENTER" or displacement >= 0.03
            if not state.counted and not state.suppressed and state.stable_frames >= required and eligible_center:
                state.counted = True
                entered += 1
                logger.info("[FLOW] Track %s stable=%s confirmed as new visitor; visitor_count +1", track_id, state.stable_frames)
        return entered, dict(self.flow_tracks)


class RuleStateRegistry:
    def __init__(self):
        self._states: dict[str, CameraRuleState] = {}

    def for_camera(self, camera_id: str) -> CameraRuleState:
        return self._states.setdefault(camera_id, CameraRuleState())

    def remove(self, camera_id: str) -> None:
        self._states.pop(camera_id, None)
