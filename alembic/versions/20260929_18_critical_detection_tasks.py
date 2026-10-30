"""Add latest-only durable queue for critical fire detection."""

from alembic import op
import sqlalchemy as sa


revision = "20260929_18"
down_revision = "20260929_17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "critical_detection_tasks" not in inspector.get_table_names():
        op.create_table(
            "critical_detection_tasks",
            sa.Column("camera_id", sa.String(length=100), nullable=False),
            sa.Column("frame_sequence", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_token", sa.String(length=36), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE", onupdate="CASCADE"),
            sa.PrimaryKeyConstraint("camera_id"),
        )
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("critical_detection_tasks")}
    if "ix_critical_detection_tasks_status" not in indexes:
        op.create_index("ix_critical_detection_tasks_status", "critical_detection_tasks", ["status"])
    if "ix_critical_detection_tasks_lease_until" not in indexes:
        op.create_index("ix_critical_detection_tasks_lease_until", "critical_detection_tasks", ["lease_until"])


def downgrade() -> None:
    op.drop_index("ix_critical_detection_tasks_lease_until", table_name="critical_detection_tasks")
    op.drop_index("ix_critical_detection_tasks_status", table_name="critical_detection_tasks")
    op.drop_table("critical_detection_tasks")
