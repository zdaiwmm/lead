"""Coordinate safe profile snapshots with browser launch and human takeover."""

from alembic import op

revision = "0004_profile_maintenance"
down_revision = "0003_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""
      CREATE TABLE lead.profile_maintenance(
        id uuid PRIMARY KEY,tenant_id uuid NOT NULL,agent_id uuid NOT NULL,profile_id uuid NOT NULL,
        purpose text NOT NULL CHECK(purpose='backup'),started_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL,state text NOT NULL CHECK(state IN ('active','complete')),
        UNIQUE(tenant_id,agent_id,id),
        FOREIGN KEY(tenant_id,agent_id,profile_id) REFERENCES lead.browser_profiles(tenant_id,agent_id,id)
      );
      CREATE UNIQUE INDEX profile_maintenance_active ON lead.profile_maintenance(profile_id) WHERE state='active';
      ALTER TABLE lead.profile_maintenance ENABLE ROW LEVEL SECURITY;
      ALTER TABLE lead.profile_maintenance FORCE ROW LEVEL SECURITY;
      CREATE POLICY agent_scope ON lead.profile_maintenance USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()));
    """)


def downgrade():
    raise RuntimeError("Profile maintenance downgrade is disabled")
