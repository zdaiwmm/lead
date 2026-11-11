"""Scoped runtime incidents and inexpensive, transactional UI revisions."""

from alembic import op

revision = "0030_fault_isolation"
down_revision = "0029_merge_channel_restore"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
    ALTER TABLE lead.admin_notifications ADD COLUMN muted boolean NOT NULL DEFAULT false;
    ALTER TABLE lead.admin_notifications ADD COLUMN external_pending boolean NOT NULL DEFAULT false;
    ALTER TABLE lead.admin_notifications ADD COLUMN external_available_at timestamptz;
    CREATE TABLE lead.runtime_issues (
      tenant_id uuid NOT NULL, agent_id uuid NOT NULL, scope text NOT NULL,
      code text NOT NULL, message text NOT NULL, needs_user boolean NOT NULL DEFAULT false,
      first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
      retry_at timestamptz, resolved_at timestamptz, occurrences integer NOT NULL DEFAULT 1,
      PRIMARY KEY(tenant_id,agent_id,scope),
      FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
    );
    CREATE TABLE lead.ui_revisions (
      tenant_id uuid NOT NULL, agent_id uuid NOT NULL, version bigint NOT NULL DEFAULT 1,
      PRIMARY KEY(tenant_id,agent_id)
    );
    DO $$ DECLARE t text; BEGIN
      FOREACH t IN ARRAY ARRAY['runtime_issues','ui_revisions'] LOOP
        EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()))',t);
        EXECUTE format('CREATE POLICY member_scope ON lead.%I AS RESTRICTIVE USING(agent_id=''00000000-0000-0000-0000-000000000000''::uuid OR lead.member_agent_allowed(agent_id)) WITH CHECK(agent_id=''00000000-0000-0000-0000-000000000000''::uuid OR lead.member_agent_allowed(agent_id))',t);
      END LOOP;
    END $$;
    DO $$ BEGIN
      IF EXISTS(SELECT FROM pg_roles WHERE rolname='lead_runtime') THEN
        GRANT SELECT,INSERT,UPDATE ON lead.runtime_issues,lead.ui_revisions TO lead_runtime;
      END IF;
    END $$;
    CREATE FUNCTION lead.touch_ui_revision() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE value jsonb; aid uuid; BEGIN
      value := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
      aid := CASE WHEN TG_TABLE_NAME='agents' THEN (value->>'id')::uuid
             ELSE coalesce((value->>'agent_id')::uuid,'00000000-0000-0000-0000-000000000000'::uuid) END;
      INSERT INTO lead.ui_revisions(tenant_id,agent_id) VALUES((value->>'tenant_id')::uuid,aid)
      ON CONFLICT(tenant_id,agent_id) DO UPDATE SET version=lead.ui_revisions.version+1;
      RETURN NULL;
    END $$;
    DO $$ DECLARE t text; BEGIN
      FOREACH t IN ARRAY ARRAY['agents','tasks','chat_jobs','conversation_messages','activity',
        'browser_runs','browser_steps','assets','runtime_issues','admin_notifications','admin_notification_receipts'] LOOP
        EXECUTE format('CREATE TRIGGER ui_changed AFTER INSERT OR UPDATE OR DELETE ON lead.%I FOR EACH ROW EXECUTE FUNCTION lead.touch_ui_revision()',t);
      END LOOP;
    END $$;
    """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("Incident history must be retained")
