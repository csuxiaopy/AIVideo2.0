"""Add explicit off-duty observations and analysis image metadata."""

from alembic import op
import sqlalchemy as sa

revision = "20260918_16"
down_revision = "20260914_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("analyses")}
    additions = (
        ("occupancy_status", sa.String(30)),
        ("off_duty_state", sa.String(40)),
        ("analysis_source", sa.String(30)),
        ("absence_started_at", sa.DateTime(timezone=True)),
        ("absence_elapsed_seconds", sa.Integer()),
    )
    for name, type_ in additions:
        if name not in columns:
            op.add_column("analyses", sa.Column(name, type_, nullable=True))
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("analyses")}
    for name in ("occupancy_status", "off_duty_state", "analysis_source"):
        index = f"ix_analyses_{name}"
        if index not in indexes:
            op.create_index(index, "analyses", [name])

    # Only VLM results are deterministic. Legacy local status=none remains unknown.
    op.execute(sa.text("""
        UPDATE analyses SET
          analysis_source = 'vlm',
          occupancy_status = CASE WHEN status = 'confirmed' THEN 'person_absent'
                                  WHEN status = 'none' THEN 'person_present'
                                  ELSE 'unknown' END,
          off_duty_state = CASE WHEN status = 'confirmed' THEN 'person_absent_confirmed'
                                WHEN status = 'none' THEN 'person_present_confirmed'
                                ELSE 'analysis_failed' END
        WHERE mode = 'off_duty' AND request_id IS NOT NULL
    """))
    op.execute(sa.text("""
        UPDATE analyses SET analysis_source = 'local_yolo', occupancy_status = 'unknown',
          off_duty_state = 'historical_unknown'
        WHERE mode = 'off_duty' AND request_id IS NULL AND occupancy_status IS NULL
    """))


def downgrade() -> None:
    pass
