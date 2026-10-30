"""Return fire detection scheduling to the isolated Redis queue."""

from alembic import op
import sqlalchemy as sa


revision = "20260929_19"
down_revision = "20260929_18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "critical_detection_tasks" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("critical_detection_tasks")


def downgrade() -> None:
    # The removed latest-only queue is intentionally not recreated on rollback.
    pass
