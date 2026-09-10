from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy as sa

from backend.capabilities import OFF_DUTY_DEFAULT_SCHEDULE
from backend.pipeline import off_duty_schedule_context
from backend.rules import CameraRuleState
from backend.schemas import CameraOptions, ScheduleSpec


def test_legacy_shift_uses_camera_fallback_value():
    schedule = ScheduleSpec.model_validate({
        "timezone": "UTC",
        "weekly": {"0": [{"start": "09:00", "end": "17:00"}]},
    })
    assert schedule.weekly["0"][0].off_duty_seconds is None
    assert CameraOptions().off_duty_seconds == 300
    assert CameraOptions().shift_grace_seconds == 0

    active, _, threshold = off_duty_schedule_context(
        schedule, 420, datetime.fromisoformat("2026-08-03T10:00:00+00:00")
    )
    assert active and threshold == 420


def test_default_periods_select_expected_thresholds():
    schedule = ScheduleSpec.model_validate(OFF_DUTY_DEFAULT_SCHEDULE)
    cases = [
        ("2026-08-03T09:00:00+08:00", 300),
        ("2026-08-03T12:00:00+08:00", 900),
        ("2026-08-03T13:30:00+08:00", 300),
    ]
    for timestamp, expected in cases:
        started_at = datetime.fromisoformat(timestamp)
        active, key, threshold = off_duty_schedule_context(schedule, 600, started_at)
        assert active and key and threshold == expected
        state = CameraRuleState()
        assert state.absence_event_update(
            False, active, threshold, started_at, schedule_key=key
        )[0] is None
        assert state.absence_event_update(
            False, active, threshold, started_at + timedelta(seconds=threshold), schedule_key=key
        )[0] == "threshold"


def test_off_duty_migration_overwrites_only_matching_cameras(monkeypatch):
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260910_11_off_duty_periods.py"
    )
    spec = importlib.util.spec_from_file_location("off_duty_period_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    cameras = sa.Table(
        "cameras",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("modes_json", sa.Text, nullable=False),
        sa.Column("schedule_json", sa.Text, nullable=False),
        sa.Column("options_json", sa.Text, nullable=False),
    )
    metadata.create_all(engine)
    original_schedule = json.dumps({"timezone": "UTC", "weekly": {}, "holidays": ["2026-01-01"]})
    with engine.begin() as connection:
        connection.execute(cameras.insert(), [
            {"id": "off", "modes_json": '["off_duty"]', "schedule_json": original_schedule,
             "options_json": '{"person_confidence":0.4,"shift_grace_seconds":60}'},
            {"id": "flow", "modes_json": '["people_flow"]', "schedule_json": original_schedule,
             "options_json": '{"shift_grace_seconds":60}'},
        ])
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()
        rows = {
            row.id: row for row in connection.execute(sa.select(cameras)).mappings().all()
        }

    migrated_schedule = json.loads(rows["off"]["schedule_json"])
    migrated_options = json.loads(rows["off"]["options_json"])
    assert set(migrated_schedule["weekly"]) == {str(day) for day in range(7)}
    assert migrated_schedule["weekly"]["0"] == migration.DEFAULT_SHIFTS
    assert migrated_schedule["holidays"] == []
    assert migrated_options == {"person_confidence": 0.4, "shift_grace_seconds": 0}
    assert rows["flow"]["schedule_json"] == original_schedule
    assert json.loads(rows["flow"]["options_json"])["shift_grace_seconds"] == 60
