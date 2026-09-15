"""Add encrypted analysis substream URL to cameras."""

from alembic import op
import sqlalchemy as sa


revision = "20260914_14"
down_revision = "20260914_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("cameras")}
    if "substream_url_encrypted" not in columns:
        op.add_column("cameras", sa.Column("substream_url_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("cameras", "substream_url_encrypted")
