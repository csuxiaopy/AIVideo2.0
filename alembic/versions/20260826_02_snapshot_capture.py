"""Add periodic frame capture state to cameras."""

from alembic import op
import sqlalchemy as sa


revision = "20260826_02"
down_revision = "20260805_01"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "cameras" not in set(inspector.get_table_names()):
        return
    columns = _columns("cameras")
    additions = [
        (
            "frame_interval_seconds",
            sa.Column("frame_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        ),
        ("last_frame_at", sa.Column("last_frame_at", sa.DateTime(timezone=True), nullable=True)),
        (
            "last_analysis_at",
            sa.Column("last_analysis_at", sa.DateTime(timezone=True), nullable=True),
        ),
    ]
    for name, column in additions:
        if name not in columns:
            op.add_column("cameras", column)


def downgrade() -> None:
    # Keep operational history and configuration on downgrade.
    pass
