"""Add the bounded-retention lookup index for the durable outbox."""

from alembic import op
import sqlalchemy as sa

revision = "20260929_17"
down_revision = "20260918_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    name = "ix_background_tasks_status_updated_id"
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("background_tasks")}
    if name in indexes:
        return
    if bind.dialect.name == "postgresql":
        # The production table contains millions of rows. Avoid blocking writes
        # while PostgreSQL builds the cleanup index.
        with op.get_context().autocommit_block():
            op.execute(sa.text(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "ix_background_tasks_status_updated_id "
                "ON background_tasks (status, updated_at, id)"
            ))
    else:
        op.create_index(name, "background_tasks", ["status", "updated_at", "id"])


def downgrade() -> None:
    # Retaining this index is safe and avoids blocking a rollback on a large table.
    pass
