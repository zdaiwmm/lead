"""Production recovery points require both database and materials off site."""

from alembic import op

revision = "0007_operations"
down_revision = "0006_telemetry"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""
      CREATE INDEX backup_kind_time ON lead.backup_records(kind,started_at DESC);
      CREATE FUNCTION lead.offsite_backup_ready() RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
        SELECT EXISTS(SELECT 1 FROM lead.backup_records d JOIN lead.backup_records f ON f.id::text=d.manifest->>'files_snapshot_id' WHERE d.kind='database' AND d.state='verified' AND f.state='verified' AND d.started_at>now()-interval '15 minutes' AND d.manifest->'remote'->>'verified'='server_sha256' AND f.manifest->'remote'->>'verified'='server_sha256')
      $$;
      REVOKE EXECUTE ON FUNCTION lead.offsite_backup_ready() FROM PUBLIC;
    """)


def downgrade():
    raise RuntimeError("Recovery freshness downgrade is disabled")
