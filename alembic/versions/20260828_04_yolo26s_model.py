"""Upgrade default general detector model to YOLO26s (conditional data migration).

- Only touches detector_settings.general_model that still equals the previous
  default "yolo26n.pt" (i.e. the operator never customized it).
- Custom weights (e.g. best.pt) are intentionally preserved.
- No schema change: the column default is applied by the ORM in backend/models.py.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260828_04"
down_revision = "20260828_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "detector_settings" not in set(inspector.get_table_names()):
        return
    op.execute(
        "UPDATE detector_settings SET general_model = 'models/yolo26s.pt' "
        "WHERE general_model = 'yolo26n.pt'"
    )


def downgrade() -> None:
    # Keep the current model on downgrade; switching back is a config decision.
    pass
