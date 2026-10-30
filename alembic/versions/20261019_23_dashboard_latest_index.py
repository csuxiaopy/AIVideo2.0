"""Cover latest-count seeks so the planner avoids the global time index."""
from alembic import op

revision = "20261019_23"
down_revision = "20261019_22"
branch_labels = None
depends_on = None


def upgrade():
    name = "ix_traffic_camera_latest_count"
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                       "ON traffic_aggregates (camera_id, bucket_start DESC) INCLUDE (current_count)")
    else:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} "
                   "ON traffic_aggregates (camera_id, bucket_start DESC, current_count)")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_traffic_camera_latest_count")
    else:
        op.execute("DROP INDEX IF EXISTS ix_traffic_camera_latest_count")
