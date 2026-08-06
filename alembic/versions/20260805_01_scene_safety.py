"""Add scene templates and safety analysis fields."""

from alembic import op
import sqlalchemy as sa


revision = "20260805_01"
down_revision = None
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "cameras" in tables:
        columns = _columns("cameras")
        if "scene_type" not in columns:
            op.add_column(
                "cameras", sa.Column("scene_type", sa.String(length=40), nullable=False, server_default="custom")
            )
            op.create_index("ix_cameras_scene_type", "cameras", ["scene_type"])

    for table, severity_default in (("analyses", "info"), ("alerts", "normal")):
        if table not in tables:
            continue
        columns = _columns(table)
        additions = [
            ("severity", sa.Column("severity", sa.String(length=20), nullable=False, server_default=severity_default)),
            ("zone_name", sa.Column("zone_name", sa.String(length=200), nullable=True)),
            ("local_model", sa.Column("local_model", sa.String(length=300), nullable=True)),
            ("model_version", sa.Column("model_version", sa.String(length=100), nullable=True)),
        ]
        for name, column in additions:
            if name not in columns:
                op.add_column(table, column)
        if "severity" not in columns:
            op.create_index(f"ix_{table}_severity", table, ["severity"])


def downgrade() -> None:
    # The monitor retains new columns on downgrade to avoid discarding alert metadata.
    pass
