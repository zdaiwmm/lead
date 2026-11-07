"""Index durable evidence independently of research task identities."""

from alembic import op

revision = "0019_operating_progress"
down_revision = "0018_pixel_test_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE INDEX operating_evidence_lookup ON lead.memory_entries
      (tenant_id,agent_id,(body->>'scope'),(body->>'key'))
      WHERE kind='observation' AND body->>'type'='operating_evidence'""")


def downgrade():
    op.execute("DROP INDEX lead.operating_evidence_lookup")
