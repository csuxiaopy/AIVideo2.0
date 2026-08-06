from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.schemas import GeometrySpec, ScheduleSpec


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


def line_side(point: tuple[float, float], line: list[tuple[float, float]]) -> float:
    if len(line) != 2:
        return 0.0
    (x1, y1), (x2, y2) = line
    x, y = point
    return (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)


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
class CameraRuleState:
    black_consecutive: int = 0
    absence_since: datetime | None = None
    positive_windows: dict[str, deque[datetime]] = field(default_factory=lambda: defaultdict(deque))
    previous_track_side: dict[int, float] = field(default_factory=dict)
    last_seen_tracks: dict[int, datetime] = field(default_factory=dict)
    counted_flow_tracks: set[int] = field(default_factory=set)
    flow_day: str | None = None
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
        self, tracks: list[tuple[int, tuple[float, float]]], line: list[tuple[float, float]], now: datetime
    ) -> tuple[int, int]:
        day = now.date().isoformat()
        if self.flow_day != day:
            self.flow_day = day
            self.counted_flow_tracks.clear()
            self.previous_track_side.clear()
            self.last_seen_tracks.clear()
        entered = exited = 0
        active_ids = set()
        for track_id, center in tracks:
            active_ids.add(track_id)
            side = line_side(center, line)
            previous = self.previous_track_side.get(track_id)
            if (
                track_id not in self.counted_flow_tracks
                and previous is not None
                and abs(previous) > 1e-5
                and abs(side) > 1e-5
                and previous * side < 0
            ):
                if previous < side:
                    entered += 1
                else:
                    exited += 1
                self.counted_flow_tracks.add(track_id)
            self.previous_track_side[track_id] = side
            self.last_seen_tracks[track_id] = now
        expiry = now - timedelta(seconds=10)
        for track_id, seen in list(self.last_seen_tracks.items()):
            if seen < expiry:
                self.last_seen_tracks.pop(track_id, None)
                self.previous_track_side.pop(track_id, None)
        return entered, exited


class RuleStateRegistry:
    def __init__(self):
        self._states: dict[str, CameraRuleState] = {}

    def for_camera(self, camera_id: str) -> CameraRuleState:
        return self._states.setdefault(camera_id, CameraRuleState())

    def remove(self, camera_id: str) -> None:
        self._states.pop(camera_id, None)
