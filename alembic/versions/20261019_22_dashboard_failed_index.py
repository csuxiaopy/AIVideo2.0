"""Index only failed analyses for dashboard date-range counts."""
from alembic import op

revision = "20261019_22"
down_revision = "20261019_21"
branch_labels = None
depends_on = None


def upgrade():
    statement = "IF NOT EXISTS ix_analyses_failed_created_at ON analyses (created_at) WHERE error IS NOT NULL"
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("CREATE INDEX CONCURRENTLY " + statement)
    else:
        op.execute("CREATE INDEX " + statement)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_analyses_failed_created_at")
    else:
        op.execute("DROP INDEX IF EXISTS ix_analyses_failed_created_at")
