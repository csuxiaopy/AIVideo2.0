"""Add intrusion schedules and camera directories."""

from alembic import op
import sqlalchemy as sa


revision = "20260909_08"
down_revision = "20260907_07"
branch_labels = None
depends_on = None


DEFAULT_INTRUSION_SCHEDULE = '{"timezone":"Asia/Shanghai","weekly":{"0":[{"start":"20:00","end":"05:00"}],"1":[{"start":"20:00","end":"05:00"}],"2":[{"start":"20:00","end":"05:00"}],"3":[{"start":"20:00","end":"05:00"}],"4":[{"start":"20:00","end":"05:00"}],"5":[{"start":"20:00","end":"05:00"}],"6":[{"start":"20:00","end":"05:00"}]},"holidays":[]}'


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "camera_directories" not in tables:
        op.create_table(
            "camera_directories",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(200), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    camera_columns = {column["name"] for column in sa.inspect(bind).get_columns("cameras")}
    if "intrusion_schedule_json" not in camera_columns:
        op.add_column("cameras", sa.Column("intrusion_schedule_json", sa.Text(), nullable=False, server_default=DEFAULT_INTRUSION_SCHEDULE))
    if "directory_id" not in camera_columns:
        op.add_column("cameras", sa.Column("directory_id", sa.Integer(), nullable=True))
        op.create_index("ix_cameras_directory_id", "cameras", ["directory_id"])
        op.create_foreign_key("fk_cameras_directory_id", "cameras", "camera_directories", ["directory_id"], ["id"], ondelete="SET NULL")
    op.execute(sa.text("UPDATE cameras SET intrusion_schedule_json = :schedule").bindparams(schedule=DEFAULT_INTRUSION_SCHEDULE))


def downgrade() -> None:
    pass
