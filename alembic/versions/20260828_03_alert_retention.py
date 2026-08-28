"""Add alert retention settings table."""

from alembic import op
import sqlalchemy as sa


revision = "20260828_03"
down_revision = "20260826_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "retention_settings" not in set(inspector.get_table_names()):
        op.create_table(
            "retention_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("alert_retention_days", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("auto_cleanup_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    # Keep retention settings on downgrade.
    pass
