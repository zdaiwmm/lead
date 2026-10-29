"""Keep post-snapshot external content identities across cutover."""

from alembic import op

revision = "0009_recovery_observations"
down_revision = "0008_local_product"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""
      CREATE TABLE lead.recovery_observations(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,agent_id uuid NOT NULL,
        target_id uuid NOT NULL,case_id uuid NOT NULL,content_hash text NOT NULL,proof jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
        UNIQUE(case_id,target_id,content_hash)
      );
      ALTER TABLE lead.recovery_observations ENABLE ROW LEVEL SECURITY;
      ALTER TABLE lead.recovery_observations FORCE ROW LEVEL SECURITY;
      CREATE POLICY scoped ON lead.recovery_observations USING(tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent()) WITH CHECK(tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent());
      REVOKE ALL ON lead.recovery_observations FROM PUBLIC;
    """)


def downgrade():
    raise RuntimeError("External observations must be retained")
