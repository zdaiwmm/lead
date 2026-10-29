"""Durable trace propagation, backup freshness and deduplicated operator alerts."""

from alembic import op

revision = "0006_telemetry"
down_revision = "0005_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""
      ALTER TABLE lead.chat_jobs ADD COLUMN trace_parent text;
      ALTER TABLE lead.tasks ADD COLUMN trace_parent text;
      ALTER TABLE lead.outbox_events ADD COLUMN trace_parent text;
      ALTER TABLE lead.backup_records ADD COLUMN state text NOT NULL DEFAULT 'verified' CHECK(state IN ('verified','unavailable','expired'));
      CREATE TABLE lead.ops_alerts(
        event_key text PRIMARY KEY,code text NOT NULL,severity text NOT NULL,
        state text NOT NULL CHECK(state IN ('active','resolved')),
        first_seen_at timestamptz NOT NULL,last_seen_at timestamptz NOT NULL,
        resolved_at timestamptz,occurrences integer NOT NULL DEFAULT 1,details jsonb NOT NULL DEFAULT '{}'
      );
      REVOKE ALL ON lead.ops_alerts FROM PUBLIC;
      CREATE FUNCTION lead.backup_ready() RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
        SELECT EXISTS(SELECT 1 FROM lead.backup_records WHERE kind='database' AND state='verified' AND completed_at>now()-interval '15 minutes')
      $$;
      REVOKE EXECUTE ON FUNCTION lead.backup_ready() FROM PUBLIC;
    """)


def downgrade():
    raise RuntimeError("Telemetry and backup audit downgrade is disabled")
