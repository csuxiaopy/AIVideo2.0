"""Durable model outage state and model replacement audit."""
from alembic import op
import sqlalchemy as sa

revision = "20261019_24"
down_revision = "20261019_23"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "auto_failover_enabled" not in {c["name"] for c in sa.inspect(bind).get_columns("model_settings")}:
        op.add_column("model_settings", sa.Column("auto_failover_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    from backend.models import ModelFailoverState, ModelSwitchLog
    ModelSwitchLog.__table__.create(bind, checkfirst=True)
    ModelFailoverState.__table__.create(bind, checkfirst=True)
    statement = "IF NOT EXISTS ix_model_calls_model_outcome_created ON model_call_logs (model, outcome, created_at DESC)"
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("CREATE INDEX CONCURRENTLY " + statement)
    else:
        op.execute("CREATE INDEX " + statement)


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_model_calls_model_outcome_created")
    op.drop_table("model_failover_state")
    op.drop_table("model_switch_logs")
    op.drop_column("model_settings", "auto_failover_enabled")
