"""Workspace-scoped configuration heads and immutable revision history."""

from alembic import op

revision = "0012_admin_configuration"
down_revision = "0011_channel_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""
      CREATE TABLE lead.admin_configs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES lead.tenants(id),
        agent_id uuid,
        config_key text NOT NULL,
        kind text NOT NULL CHECK(kind IN ('budget','strategy','onboarding')),
        version integer NOT NULL DEFAULT 0,
        published_version integer NOT NULL DEFAULT 0,
        body jsonb,
        draft jsonb,
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(tenant_id,config_key),
        UNIQUE(tenant_id,id),
        FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
      );
      CREATE TABLE lead.admin_config_versions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL,
        agent_id uuid,
        config_id uuid NOT NULL,
        config_key text NOT NULL,
        version integer NOT NULL,
        action text NOT NULL CHECK(action IN ('draft','publish','rollback')),
        before_state jsonb NOT NULL,
        after_state jsonb NOT NULL,
        actor_ref text NOT NULL,
        note text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(config_id,version),
        FOREIGN KEY(tenant_id,config_id) REFERENCES lead.admin_configs(tenant_id,id),
        FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
      );
      CREATE INDEX admin_versions_time ON lead.admin_config_versions(tenant_id,created_at DESC,id);
      ALTER TABLE lead.admin_configs ENABLE ROW LEVEL SECURITY;
      ALTER TABLE lead.admin_configs FORCE ROW LEVEL SECURITY;
      ALTER TABLE lead.admin_config_versions ENABLE ROW LEVEL SECURITY;
      ALTER TABLE lead.admin_config_versions FORCE ROW LEVEL SECURITY;
      CREATE POLICY config_read ON lead.admin_configs FOR SELECT USING(
        tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id IS NULL OR agent_id=lead.ctx_agent()));
      CREATE POLICY config_insert ON lead.admin_configs FOR INSERT WITH CHECK(
        tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()));
      CREATE POLICY config_update ON lead.admin_configs FOR UPDATE USING(
        tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(
        tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()));
      CREATE POLICY version_read ON lead.admin_config_versions FOR SELECT USING(
        tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id IS NULL OR agent_id=lead.ctx_agent()));
      CREATE POLICY version_insert ON lead.admin_config_versions FOR INSERT WITH CHECK(
        tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()));
    """)


def downgrade():
    raise RuntimeError("Configuration history must be preserved")
