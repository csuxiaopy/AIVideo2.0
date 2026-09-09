"""Use one vision model for phone-use and smoking detection."""

from alembic import op
import sqlalchemy as sa


revision = "20260909_10"
down_revision = "20260909_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("model_settings")}
    if "enhanced_model" in columns:
        with op.batch_alter_table("model_settings") as batch_op:
            batch_op.drop_column("enhanced_model")


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("model_settings")}
    if "enhanced_model" not in columns:
        with op.batch_alter_table("model_settings") as batch_op:
            batch_op.add_column(sa.Column(
                "enhanced_model", sa.String(200), nullable=False,
                server_default="qwen3.7-plus",
            ))
