"""Reset existing fire/smoke camera thresholds to 0.90."""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "20260914_13"
down_revision = "20260910_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "cameras" not in set(sa.inspect(bind).get_table_names()):
        return

    cameras = sa.table(
        "cameras",
        sa.column("id", sa.String()),
        sa.column("modes_json", sa.Text()),
        sa.column("options_json", sa.Text()),
    )
    rows = bind.execute(
        sa.select(cameras.c.id, cameras.c.modes_json, cameras.c.options_json)
    ).mappings()
    for row in rows:
        try:
            modes = json.loads(row["modes_json"] or "[]")
            options = json.loads(row["options_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if "fire_smoke" not in modes or not isinstance(options, dict):
            continue
        options["fire_confidence"] = 0.90
        options["smoke_confidence"] = 0.90
        bind.execute(
            cameras.update()
            .where(cameras.c.id == row["id"])
            .values(options_json=json.dumps(options, ensure_ascii=False, separators=(",", ":")))
        )


def downgrade() -> None:
    # Existing camera settings are user data and are intentionally not reverted.
    pass
