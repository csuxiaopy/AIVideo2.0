"""Add administrator audit, analysis and model-call logs."""

from alembic import op
import sqlalchemy as sa


revision = "20260909_09"
down_revision = "20260909_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "audit_logs" not in tables:
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("actor_username", sa.String(64), nullable=False, server_default=""),
            sa.Column("actor_display_name", sa.String(100), nullable=False, server_default=""),
            sa.Column("action", sa.String(100), nullable=False),
            sa.Column("target_type", sa.String(80), nullable=False, server_default=""),
            sa.Column("target_id", sa.String(200), nullable=False, server_default=""),
            sa.Column("method", sa.String(10), nullable=False),
            sa.Column("path", sa.String(500), nullable=False),
            sa.Column("outcome", sa.String(20), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("ip_address", sa.String(100), nullable=False, server_default=""),
            sa.Column("user_agent", sa.String(500), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        )
        for column in ("actor_user_id", "actor_username", "action", "target_type", "outcome", "created_at"):
            op.create_index(f"ix_audit_logs_{column}", "audit_logs", [column])

    if "model_call_logs" not in tables:
        op.create_table(
            "model_call_logs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("camera_id", sa.String(100), nullable=True),
            sa.Column("camera_name", sa.String(200), nullable=False, server_default=""),
            sa.Column("modes_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("stage", sa.String(20), nullable=False),
            sa.Column("provider", sa.String(100), nullable=False, server_default=""),
            sa.Column("model", sa.String(200), nullable=False, server_default=""),
            sa.Column("request_id", sa.String(200), nullable=True),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column("outcome", sa.String(20), nullable=False),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("usage_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("raw_response", sa.Text(), nullable=False, server_default=""),
            sa.Column("parsed_response_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="SET NULL", onupdate="CASCADE"),
        )
        for column in ("camera_id", "stage", "model", "request_id", "outcome", "created_at"):
            op.create_index(f"ix_model_call_logs_{column}", "model_call_logs", [column])

    analysis_columns = {column["name"] for column in sa.inspect(bind).get_columns("analyses")}
    if "camera_name" not in analysis_columns:
        op.add_column("analyses", sa.Column("camera_name", sa.String(200), nullable=False, server_default=""))
        op.execute(sa.text(
            "UPDATE analyses SET camera_name = COALESCE((SELECT name FROM cameras WHERE cameras.id = analyses.camera_id), camera_id, '')"
        ))

    foreign_keys = sa.inspect(bind).get_foreign_keys("analyses")
    camera_fk = next((fk for fk in foreign_keys if fk.get("constrained_columns") == ["camera_id"]), None)
    if camera_fk and camera_fk.get("options", {}).get("ondelete") != "SET NULL":
        name = camera_fk.get("name")
        if name:
            op.drop_constraint(name, "analyses", type_="foreignkey")
            op.alter_column("analyses", "camera_id", existing_type=sa.String(100), nullable=True)
            op.create_foreign_key(
                "fk_analyses_camera_id", "analyses", "cameras", ["camera_id"], ["id"],
                ondelete="SET NULL", onupdate="CASCADE",
            )

    retention_columns = {column["name"] for column in sa.inspect(bind).get_columns("retention_settings")}
    if "log_retention_days" not in retention_columns:
        op.add_column(
            "retention_settings",
            sa.Column("log_retention_days", sa.Integer(), nullable=False, server_default="30"),
        )


def downgrade() -> None:
    pass
