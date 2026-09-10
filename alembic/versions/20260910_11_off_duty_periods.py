"""Apply per-period off-duty defaults to existing off-duty cameras."""

import json

from alembic import op
import sqlalchemy as sa


revision = "20260910_11"
down_revision = "20260909_10"
branch_labels = None
depends_on = None


DEFAULT_SHIFTS = [
    {"start": "09:00", "end": "11:00", "off_duty_seconds": 300},
    {"start": "12:00", "end": "13:30", "off_duty_seconds": 900},
    {"start": "13:30", "end": "17:00", "off_duty_seconds": 300},
]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def upgrade() -> None:
    bind = op.get_bind()
    cameras = sa.table(
        "cameras",
        sa.column("id", sa.String()),
        sa.column("modes_json", sa.Text()),
        sa.column("schedule_json", sa.Text()),
        sa.column("options_json", sa.Text()),
    )
    default_schedule = {
        "timezone": "Asia/Shanghai",
        "weekly": {str(day): DEFAULT_SHIFTS for day in range(7)},
        "holidays": [],
    }
    rows = bind.execute(
        sa.select(cameras.c.id, cameras.c.modes_json, cameras.c.options_json)
    ).mappings().all()
    for row in rows:
        try:
            modes = json.loads(row["modes_json"] or "[]")
        except (TypeError, ValueError):
            continue
        if "off_duty" not in modes:
            continue
        try:
            options = json.loads(row["options_json"] or "{}")
        except (TypeError, ValueError):
            options = {}
        if not isinstance(options, dict):
            options = {}
        options["shift_grace_seconds"] = 0
        bind.execute(
            cameras.update()
            .where(cameras.c.id == row["id"])
            .values(schedule_json=_json(default_schedule), options_json=_json(options))
        )


def downgrade() -> None:
    # The prior per-camera schedules were intentionally overwritten and cannot
    # be reconstructed safely. Keep the migrated settings on downgrade.
    pass
