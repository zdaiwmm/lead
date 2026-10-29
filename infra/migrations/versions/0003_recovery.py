"""Restored databases start with external writes fenced."""

from alembic import op

revision = "0003_recovery"
down_revision = "0002_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""
    CREATE TABLE lead.recovery_control(
      singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
      state text NOT NULL DEFAULT 'normal' CHECK(state IN ('normal','recovery_required')),
      backup_id uuid,restored_at timestamptz,recovery_window_start timestamptz,
      updated_at timestamptz NOT NULL DEFAULT now()
    );
    INSERT INTO lead.recovery_control(singleton) VALUES(true);
    CREATE TABLE lead.backup_records(
      id uuid PRIMARY KEY,kind text NOT NULL,agent_id uuid,
      started_at timestamptz NOT NULL,completed_at timestamptz NOT NULL,
      cipher_sha256 text NOT NULL,key_id text NOT NULL,size_bytes bigint NOT NULL,
      object_ref text NOT NULL,manifest jsonb NOT NULL
    );
    REVOKE ALL ON lead.recovery_control,lead.backup_records FROM PUBLIC;
    """)


def downgrade():
    raise RuntimeError("Recovery ledger downgrade is disabled")
