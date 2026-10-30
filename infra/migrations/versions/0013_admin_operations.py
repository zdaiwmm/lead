"""Operational settings, bounded runtime leases and workspace notifications."""

from alembic import op

revision = "0013_admin_operations"
down_revision = "0012_admin_configuration"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
      ALTER TABLE lead.admin_configs DROP CONSTRAINT admin_configs_kind_check;
      ALTER TABLE lead.admin_configs ADD CHECK(kind IN ('budget','strategy','onboarding','models','brand','runtime','notifications'));
      CREATE TABLE lead.admin_runtime_state (
        tenant_id uuid NOT NULL, agent_id uuid NOT NULL, category text NOT NULL,
        failures integer NOT NULL DEFAULT 0, last_error text, next_retry_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(tenant_id,agent_id,category),
        FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
      );
      CREATE TABLE lead.admin_runtime_leases (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL,
        agent_id uuid NOT NULL, category text NOT NULL, expires_at timestamptz NOT NULL,
        released_at timestamptz,
        FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
      );
      CREATE INDEX admin_runtime_active ON lead.admin_runtime_leases(tenant_id,expires_at) WHERE released_at IS NULL;
      CREATE TABLE lead.admin_notification_conditions (
        tenant_id uuid NOT NULL REFERENCES lead.tenants(id), condition_key text NOT NULL,
        active boolean NOT NULL DEFAULT false, episode integer NOT NULL DEFAULT 0,
        last_notified_at timestamptz, PRIMARY KEY(tenant_id,condition_key)
      );
      CREATE TABLE lead.admin_notifications (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL,
        agent_id uuid, condition_key text NOT NULL, episode integer NOT NULL,
        category text NOT NULL, severity text NOT NULL, title text NOT NULL, message text NOT NULL,
        recipient_roles jsonb NOT NULL, recipient_subjects jsonb NOT NULL DEFAULT '[]',
        created_at timestamptz NOT NULL DEFAULT now(), available_at timestamptz NOT NULL,
        resolved_at timestamptz,
        UNIQUE(tenant_id,condition_key,episode), UNIQUE(tenant_id,id),
        FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
      );
      CREATE INDEX admin_notifications_time ON lead.admin_notifications(tenant_id,created_at DESC,id DESC);
      CREATE TABLE lead.admin_notification_receipts (
        tenant_id uuid NOT NULL, notification_id uuid NOT NULL, actor_ref text NOT NULL,
        read_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,notification_id,actor_ref),
        FOREIGN KEY(tenant_id,notification_id) REFERENCES lead.admin_notifications(tenant_id,id)
      );
      DO $$ DECLARE t text; BEGIN
        FOREACH t IN ARRAY ARRAY['admin_runtime_state','admin_runtime_leases','admin_notifications'] LOOP
          EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',t);
          EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',t);
          EXECUTE format('CREATE POLICY scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()))',t);
        END LOOP;
        FOREACH t IN ARRAY ARRAY['admin_notification_conditions','admin_notification_receipts'] LOOP
          EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',t);
          EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',t);
          EXECUTE format('CREATE POLICY scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND lead.ctx_agent() IS NULL) WITH CHECK(tenant_id=lead.ctx_tenant() AND lead.ctx_agent() IS NULL)',t);
        END LOOP;
      END $$;
    """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("Operational settings and notification history must be retained")
