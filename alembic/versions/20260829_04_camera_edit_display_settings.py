"""Add display settings and preserve camera history on business ID rename."""

from alembic import op
import sqlalchemy as sa


revision = "20260829_04"
down_revision = "20260828_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "display_settings" not in set(inspector.get_table_names()):
        op.create_table(
            "display_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("show_traffic_report", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("show_current_store_count", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if bind.dialect.name == "postgresql":
        for table in ("analyses", "alerts", "traffic_aggregates"):
            foreign_keys = sa.inspect(bind).get_foreign_keys(table)
            for foreign_key in foreign_keys:
                if foreign_key.get("referred_table") == "cameras" and foreign_key.get("constrained_columns") == ["camera_id"]:
                    if foreign_key.get("name"):
                        op.drop_constraint(foreign_key["name"], table, type_="foreignkey")
                    break
            op.create_foreign_key(
                f"fk_{table}_camera_id_cameras",
                table,
                "cameras",
                ["camera_id"],
                ["id"],
                ondelete="CASCADE",
                onupdate="CASCADE",
            )


def downgrade() -> None:
    # Keep user display preferences and cascading history protection on downgrade.
    pass
