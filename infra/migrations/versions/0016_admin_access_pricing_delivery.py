"""Member scopes, immutable pricing snapshots and durable external delivery."""

from alembic import op

revision = "0016_admin_administration"
down_revision = "0015_admin_model_catalog"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
      ALTER TABLE lead.admin_configs DROP CONSTRAINT admin_configs_kind_check;
      ALTER TABLE lead.admin_configs ADD CHECK(kind IN ('budget','strategy','onboarding','models','brand','runtime','notifications','channel_rule','attribution','experience','model_catalog','member_access','pricing','external_notifications'));
      ALTER TABLE lead.budget_reservations ADD COLUMN pricing_snapshot jsonb;
      CREATE FUNCTION lead.member_agent_allowed(p_agent uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
        SELECT CASE WHEN p_agent IS NULL OR coalesce(nullif(current_setting('app.allowed_agent_ids',true),''),'*')='*' THEN true
          ELSE current_setting('app.allowed_agent_ids',true)::jsonb ? p_agent::text END
      $$;
      DO $$ DECLARE item record; BEGIN
        FOR item IN SELECT table_name FROM information_schema.columns WHERE table_schema='lead' AND column_name='agent_id' LOOP
          EXECUTE format('CREATE POLICY member_agent_scope ON lead.%I AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id))',item.table_name);
        END LOOP;
      END $$;
      CREATE POLICY member_agent_scope ON lead.agents AS RESTRICTIVE USING(lead.member_agent_allowed(id)) WITH CHECK(lead.member_agent_allowed(id));
      CREATE TABLE lead.notification_secrets(
        tenant_id uuid NOT NULL REFERENCES lead.tenants(id), destination_id uuid NOT NULL,
        encrypted_payload bytea NOT NULL, version bigint NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(tenant_id,destination_id)
      );
      CREATE TABLE lead.notification_deliveries(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL REFERENCES lead.tenants(id),agent_id uuid,
        notification_id uuid NOT NULL,destination_id uuid NOT NULL,configuration_version text NOT NULL,destination_snapshot jsonb NOT NULL,
        state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','retry','delivered','failed','unknown','skipped')),
        attempts integer NOT NULL DEFAULT 0, attempt_limit integer NOT NULL DEFAULT 3,available_at timestamptz NOT NULL,lease_until timestamptz,
        attempt_token uuid,last_status_code integer,last_error_code text,completed_at timestamptz,created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(tenant_id,notification_id,destination_id),
        FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
      );
      CREATE INDEX notification_delivery_due ON lead.notification_deliveries(tenant_id,state,available_at);
      ALTER TABLE lead.notification_secrets ENABLE ROW LEVEL SECURITY;
      ALTER TABLE lead.notification_secrets FORCE ROW LEVEL SECURITY;
      CREATE POLICY workspace_scope ON lead.notification_secrets USING(tenant_id=lead.ctx_tenant() AND lead.ctx_agent() IS NULL) WITH CHECK(tenant_id=lead.ctx_tenant() AND lead.ctx_agent() IS NULL);
      ALTER TABLE lead.notification_deliveries ENABLE ROW LEVEL SECURITY;
      ALTER TABLE lead.notification_deliveries FORCE ROW LEVEL SECURITY;
      CREATE POLICY workspace_scope ON lead.notification_deliveries USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()));
      CREATE POLICY member_agent_scope ON lead.notification_deliveries AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id));
    """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError(
        "Member configuration, pricing evidence and delivery history must be retained"
    )
