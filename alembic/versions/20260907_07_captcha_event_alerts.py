"""add captcha challenges and alert event metadata

Revision ID: 20260907_07
Revises: 20260903_06
"""
from alembic import op
import sqlalchemy as sa

revision = "20260907_07"
down_revision = "20260903_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    alert_columns = {column["name"] for column in inspector.get_columns("alerts")}
    if "event_phase" not in alert_columns:
        op.add_column("alerts", sa.Column("event_phase", sa.String(30), nullable=True))
    if "event_started_at" not in alert_columns:
        op.add_column("alerts", sa.Column("event_started_at", sa.DateTime(timezone=True), nullable=True))
    if "event_ended_at" not in alert_columns:
        op.add_column("alerts", sa.Column("event_ended_at", sa.DateTime(timezone=True), nullable=True))
    if "captcha_challenges" not in set(inspector.get_table_names()):
        op.create_table(
            "captcha_challenges",
            sa.Column("id", sa.String(64), nullable=False),
            sa.Column("answer_hash", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_captcha_challenges_expires_at", "captcha_challenges", ["expires_at"])
    # The old UI stored 15 seconds while runtime actually used a fixed 180-second cadence.
    if bind.dialect.name == "postgresql":
        op.execute("""
            UPDATE cameras
            SET options_json = jsonb_set(options_json::jsonb, '{behavior_interval_seconds}', '180')::text
            WHERE COALESCE((options_json::jsonb->>'behavior_interval_seconds')::int, 180) < 60
        """)


def downgrade() -> None:
    op.drop_table("captcha_challenges")
    op.drop_column("alerts", "event_ended_at")
    op.drop_column("alerts", "event_started_at")
    op.drop_column("alerts", "event_phase")
