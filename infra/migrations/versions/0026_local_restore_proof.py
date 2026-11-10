"""Expose a verified local restore separately from independently stored backups."""

from alembic import op

revision = "0026_local_restore_proof"
down_revision = "0025_site_devices"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""
        CREATE FUNCTION lead.local_restore_ready() RETURNS boolean LANGUAGE sql
        SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
          SELECT EXISTS(
            SELECT 1 FROM lead.backup_records d JOIN lead.backup_records f
              ON f.id::text=d.manifest->>'files_snapshot_id'
            WHERE d.kind='database' AND f.kind='data_files'
              AND d.state='verified' AND f.state='verified'
              AND d.started_at BETWEEN now()-interval '15 minutes' AND now()
              AND d.manifest#>>'{local_restore,kind}'='isolated_postgres'
              AND (d.manifest#>>'{local_restore,checked_at}')::timestamptz
                    BETWEEN d.started_at AND now()
              AND d.manifest#>>'{local_restore,cipher_sha256}'=d.cipher_sha256
              AND d.manifest#>>'{local_restore,files_sha256}'=f.cipher_sha256
              AND d.manifest->>'files_snapshot_sha256'=f.cipher_sha256
              AND jsonb_typeof(d.manifest->'verification_counts')='object'
              AND d.manifest->'verification_counts'=d.manifest#>'{local_restore,restored_counts}')
        $$;
        REVOKE ALL ON FUNCTION lead.local_restore_ready() FROM PUBLIC;
        DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lead_runtime') THEN
          GRANT EXECUTE ON FUNCTION lead.local_restore_ready() TO lead_runtime;
        END IF; END $$;
    """)


def downgrade():
    op.get_bind().exec_driver_sql("DROP FUNCTION lead.local_restore_ready()")
