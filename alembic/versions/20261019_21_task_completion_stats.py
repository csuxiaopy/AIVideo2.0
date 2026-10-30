"""Incremental completion watermarks and bounded task-status queries."""
from alembic import op
import sqlalchemy as sa

revision = "20261019_21"
down_revision = "20260929_20"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    table = sa.Table("background_task_completions", sa.MetaData(),
        sa.Column("kind", sa.String(40), primary_key=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=False))
    table.create(bind, checkfirst=True)
    # Build without blocking ongoing analysis writes on PostgreSQL.
    indexes = [
        "ix_background_tasks_completed_kind_updated",
        "ix_background_tasks_unfinished_kind_status_created",
    ]
    definitions = [
        "ON background_tasks (kind, updated_at DESC) WHERE status = 'completed'",
        "ON background_tasks (kind, status, created_at) WHERE status <> 'completed'",
    ]
    if postgres:
        with op.get_context().autocommit_block():
            for name, definition in zip(indexes, definitions):
                op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")
    else:
        for name, definition in zip(indexes, definitions):
            op.execute(f"CREATE INDEX IF NOT EXISTS {name} {definition}")

    if postgres:
        op.execute("""
            CREATE OR REPLACE FUNCTION update_background_task_completion()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO background_task_completions (kind, last_completed_at)
                VALUES (NEW.kind, NEW.updated_at)
                ON CONFLICT (kind) DO UPDATE
                SET last_completed_at = EXCLUDED.last_completed_at
                WHERE background_task_completions.last_completed_at < EXCLUDED.last_completed_at;
                RETURN NEW;
            END $$
        """)
        op.execute("DROP TRIGGER IF EXISTS background_task_completion_watermark ON background_tasks")
        op.execute("""
            CREATE TRIGGER background_task_completion_watermark
            AFTER INSERT OR UPDATE OF status, updated_at ON background_tasks
            FOR EACH ROW WHEN (NEW.status = 'completed')
            EXECUTE FUNCTION update_background_task_completion()
        """)
    elif bind.dialect.name == "sqlite":
        for event, suffix in [("INSERT", "insert"), ("UPDATE OF status, updated_at", "update")]:
            op.execute(f"""
                CREATE TRIGGER IF NOT EXISTS background_task_completion_{suffix}
                AFTER {event} ON background_tasks
                FOR EACH ROW WHEN NEW.status = 'completed'
                BEGIN
                    INSERT INTO background_task_completions (kind, last_completed_at)
                    VALUES (NEW.kind, NEW.updated_at)
                    ON CONFLICT (kind) DO UPDATE SET last_completed_at = excluded.last_completed_at
                    WHERE background_task_completions.last_completed_at < excluded.last_completed_at;
                END
            """)
    else:
        raise RuntimeError("Task completion statistics require PostgreSQL or SQLite")

    # One-time bootstrap: enumerate kinds once, then seek the latest indexed row
    # per kind. The trigger and conflict condition protect concurrent updates.
    op.execute("""
        INSERT INTO background_task_completions (kind, last_completed_at)
        SELECT kinds.kind, (
            SELECT updated_at FROM background_tasks AS latest
            WHERE latest.kind = kinds.kind AND latest.status = 'completed'
            ORDER BY updated_at DESC LIMIT 1
        ) FROM (SELECT DISTINCT kind FROM background_tasks WHERE status = 'completed') AS kinds
        WHERE 1 = 1
        ON CONFLICT (kind) DO UPDATE SET last_completed_at = EXCLUDED.last_completed_at
        WHERE background_task_completions.last_completed_at < EXCLUDED.last_completed_at
    """)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS background_task_completion_watermark ON background_tasks")
        op.execute("DROP FUNCTION IF EXISTS update_background_task_completion()")
    else:
        op.execute("DROP TRIGGER IF EXISTS background_task_completion_insert")
        op.execute("DROP TRIGGER IF EXISTS background_task_completion_update")
    op.execute("DROP INDEX IF EXISTS ix_background_tasks_completed_kind_updated")
    op.execute("DROP INDEX IF EXISTS ix_background_tasks_unfinished_kind_status_created")
    op.drop_table("background_task_completions")
