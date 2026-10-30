"""Connection-wide pixel test sessions with explicit termination."""

from alembic import op

revision = "0018_pixel_test_sessions"
down_revision = "0017_pixel_events"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE lead.pixel_tests
          ALTER COLUMN definition_id DROP NOT NULL,
          ALTER COLUMN definition_revision DROP NOT NULL,
          ADD COLUMN definition_revisions jsonb NOT NULL DEFAULT '{}',
          ADD COLUMN ended_at timestamptz;
        UPDATE lead.pixel_tests t SET definition_revisions=jsonb_build_object(d.name,t.definition_revision)
          FROM lead.pixel_definitions d WHERE d.id=t.definition_id;
        CREATE INDEX pixel_test_sessions ON lead.pixel_tests(connection_id,created_at DESC);
        """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("Pixel test session history must be retained")
