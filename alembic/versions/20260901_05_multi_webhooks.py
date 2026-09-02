"""Add multiple webhook targets and per-alert delivery records."""

from alembic import op
import sqlalchemy as sa


revision = "20260901_05"
down_revision = "20260829_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "webhook_targets" not in tables:
        op.create_table(
            "webhook_targets",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("url", sa.Text(), nullable=False, server_default=""),
            sa.Column("secret_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("auto_severities_json", sa.Text(), nullable=False, server_default='["normal","high","critical"]'),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    tables = set(sa.inspect(bind).get_table_names())
    target_count = bind.execute(sa.text("SELECT COUNT(*) FROM webhook_targets")).scalar()
    if not target_count and "webhook_settings" in tables:
        old = bind.execute(sa.text(
            "SELECT enabled, url, secret_encrypted, updated_at FROM webhook_settings WHERE id = 1"
        )).mappings().first()
        if old and (old["enabled"] or old["url"] or old["secret_encrypted"]):
            bind.execute(sa.text(
                "INSERT INTO webhook_targets "
                "(name, enabled, url, secret_encrypted, auto_severities_json, created_at, updated_at) "
                "VALUES (:name, :enabled, :url, :secret, :levels, :created, :updated)"
            ), {
                "name": "默认 Webhook", "enabled": old["enabled"], "url": old["url"] or "",
                "secret": old["secret_encrypted"] or "", "levels": '["normal","high","critical"]',
                "created": old["updated_at"], "updated": old["updated_at"],
            })
    tables = set(sa.inspect(bind).get_table_names())
    if "webhook_deliveries" not in tables:
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("alert_id", sa.Integer(), sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("webhook_target_id", sa.Integer(), sa.ForeignKey("webhook_targets.id", ondelete="SET NULL")),
            sa.Column("target_name", sa.String(200), nullable=False),
            sa.Column("target_url", sa.Text(), nullable=False),
            sa.Column("trigger", sa.String(20), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("alert_id", "webhook_target_id", name="uq_alert_webhook_target"),
        )
        op.create_index("ix_webhook_deliveries_alert_id", "webhook_deliveries", ["alert_id"])
        op.create_index("ix_webhook_deliveries_webhook_target_id", "webhook_deliveries", ["webhook_target_id"])


def downgrade() -> None:
    # Preserve configured targets and delivery history on downgrade.
    pass
