"""Tune autovacuum for the high-churn background task outbox."""

from alembic import op


revision = "20260929_20"
down_revision = "20260929_19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            ALTER TABLE background_tasks SET (
                autovacuum_vacuum_scale_factor = 0.02,
                autovacuum_vacuum_threshold = 5000,
                autovacuum_analyze_scale_factor = 0.02
            )
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            ALTER TABLE background_tasks RESET (
                autovacuum_vacuum_scale_factor,
                autovacuum_vacuum_threshold,
                autovacuum_analyze_scale_factor
            )
        """)
