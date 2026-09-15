"""Add durable background outbox without changing legacy processing tables."""

from alembic import op
import sqlalchemy as sa

revision = "20260914_15"
down_revision = "20260914_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Startup may already have run metadata.create_all; also support Alembic alone.
    if "background_tasks" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "background_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(240), nullable=False, unique=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("camera_id", sa.String(100)),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("evidence_ref", sa.Text()),
        sa.Column("result_json", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("kind", "camera_id", "status"):
        op.create_index(f"ix_background_tasks_{column}", "background_tasks", [column])


def downgrade() -> None:
    # Rolling back the application must not delete pending jobs/audit history.
    pass
