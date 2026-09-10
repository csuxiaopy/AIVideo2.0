"""Add directory alert snapshots and multi-image evidence."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_12"
down_revision = "20260910_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    alert_columns = {column["name"] for column in inspector.get_columns("alerts")}
    if "directory_id" not in alert_columns:
        op.add_column("alerts", sa.Column("directory_id", sa.Integer(), nullable=True))
        op.create_index("ix_alerts_directory_id", "alerts", ["directory_id"])
    if "alert_name" not in alert_columns:
        op.add_column(
            "alerts",
            sa.Column("alert_name", sa.String(400), nullable=False, server_default=""),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "alert_evidences" not in tables:
        op.create_table(
            "alert_evidences",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("alert_id", sa.Integer(), nullable=False),
            sa.Column("camera_id", sa.String(100), nullable=False),
            sa.Column("camera_name", sa.String(200), nullable=False),
            sa.Column("evidence_path", sa.Text(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_alert_evidences_alert_id", "alert_evidences", ["alert_id"])

    op.execute(sa.text("""
        UPDATE alerts
        SET directory_id = (
            SELECT cameras.directory_id FROM cameras WHERE cameras.id = alerts.camera_id
        )
        WHERE directory_id IS NULL
    """))
    op.execute(sa.text("""
        UPDATE alerts
        SET alert_name = COALESCE(
            (SELECT camera_directories.name || '营业厅视频'
             FROM cameras
             JOIN camera_directories ON camera_directories.id = cameras.directory_id
             WHERE cameras.id = alerts.camera_id),
            '未分组营业厅视频'
        )
        WHERE alert_name = '' OR alert_name IS NULL
    """))
    op.execute(sa.text("""
        INSERT INTO alert_evidences
            (alert_id, camera_id, camera_name, evidence_path, confidence, sort_order, created_at)
        SELECT alerts.id, alerts.camera_id, cameras.name, alerts.evidence_path,
               alerts.confidence, 0, alerts.created_at
        FROM alerts
        JOIN cameras ON cameras.id = alerts.camera_id
        WHERE alerts.evidence_path IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM alert_evidences WHERE alert_evidences.alert_id = alerts.id
          )
    """))


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "alert_evidences" in tables:
        op.drop_table("alert_evidences")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("alerts")}
    if "alert_name" in columns:
        op.drop_column("alerts", "alert_name")
    if "directory_id" in columns:
        op.drop_index("ix_alerts_directory_id", table_name="alerts")
        op.drop_column("alerts", "directory_id")
