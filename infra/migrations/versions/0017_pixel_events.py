"""Website pixel onboarding, isolated test receipts and durable source snapshots."""

from alembic import op

revision = "0017_pixel_events"
down_revision = "0016_admin_administration"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
      ALTER TABLE lead.analytics_connections
        ADD COLUMN embedded_at timestamptz,
        ADD COLUMN last_pageview_at timestamptz,
        ADD COLUMN onboarding_completed_at timestamptz,
        ADD COLUMN selected_at timestamptz NOT NULL DEFAULT now();
      UPDATE lead.analytics_connections SET last_pageview_at=last_received_at,
        embedded_at=created_at,onboarding_completed_at=last_received_at WHERE state='verified';
      ALTER TABLE lead.visit_sessions ADD COLUMN anonymous_id uuid;
      CREATE TABLE lead.pixel_definitions(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,agent_id uuid NOT NULL,
        connection_id uuid NOT NULL,name text NOT NULL,label text NOT NULL,
        method text NOT NULL CHECK(method IN ('browser','server')),definition text NOT NULL DEFAULT '',
        revision integer NOT NULL DEFAULT 1,created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(tenant_id,agent_id,id),UNIQUE(connection_id,name),
        FOREIGN KEY(tenant_id,agent_id,connection_id) REFERENCES lead.analytics_connections(tenant_id,agent_id,id)
      );
      CREATE UNIQUE INDEX pixel_definition_name ON lead.pixel_definitions(connection_id,lower(name));
      CREATE TABLE lead.pixel_tests(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,agent_id uuid NOT NULL,
        connection_id uuid NOT NULL,definition_id uuid NOT NULL,definition_revision integer NOT NULL,
        code_sha256 text NOT NULL UNIQUE,created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes',received_at timestamptz,
        UNIQUE(tenant_id,agent_id,id),
        FOREIGN KEY(tenant_id,agent_id,definition_id) REFERENCES lead.pixel_definitions(tenant_id,agent_id,id)
      );
      CREATE TABLE lead.pixel_touchpoints(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,agent_id uuid NOT NULL,
        connection_id uuid NOT NULL,visit_session_id uuid NOT NULL,tracking_link_id uuid NOT NULL,
        anonymous_id uuid NOT NULL,occurred_at timestamptz NOT NULL DEFAULT now(),
        attribution_policy jsonb NOT NULL,attribution_version text NOT NULL,
        UNIQUE(tenant_id,agent_id,id),
        FOREIGN KEY(tenant_id,agent_id,connection_id) REFERENCES lead.analytics_connections(tenant_id,agent_id,id),
        FOREIGN KEY(tenant_id,agent_id,visit_session_id) REFERENCES lead.visit_sessions(tenant_id,agent_id,id),
        FOREIGN KEY(tenant_id,agent_id,tracking_link_id) REFERENCES lead.tracking_links(tenant_id,agent_id,id)
      );
      CREATE INDEX pixel_touch_visitor ON lead.pixel_touchpoints(connection_id,anonymous_id,occurred_at DESC);
      CREATE TABLE lead.pixel_identities(
        tenant_id uuid NOT NULL,agent_id uuid NOT NULL,connection_id uuid NOT NULL,
        anonymous_id uuid NOT NULL,user_key text NOT NULL,identified_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(connection_id,anonymous_id),
        FOREIGN KEY(tenant_id,agent_id,connection_id) REFERENCES lead.analytics_connections(tenant_id,agent_id,id)
      );
      CREATE TABLE lead.pixel_receipts(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,agent_id uuid NOT NULL,
        connection_id uuid NOT NULL,event_id text NOT NULL,event_name text NOT NULL,
        scope_key text NOT NULL DEFAULT 'live',anonymous_id uuid,user_key text,business_key text,
        touchpoint_id uuid,visit_session_id uuid,definition_revision integer,
        trust_level text NOT NULL CHECK(trust_level IN ('client_observed','server_verified')),
        counted boolean NOT NULL DEFAULT true,duplicate_of uuid,payload_sha256 text NOT NULL,
        occurred_at timestamptz NOT NULL,received_at timestamptz NOT NULL DEFAULT now(),
        value numeric(20,6),currency char(3),
        UNIQUE(connection_id,scope_key,event_id),
        FOREIGN KEY(tenant_id,agent_id,connection_id) REFERENCES lead.analytics_connections(tenant_id,agent_id,id),
        FOREIGN KEY(tenant_id,agent_id,touchpoint_id) REFERENCES lead.pixel_touchpoints(tenant_id,agent_id,id),
        FOREIGN KEY(tenant_id,agent_id,visit_session_id) REFERENCES lead.visit_sessions(tenant_id,agent_id,id)
      );
      CREATE UNIQUE INDEX pixel_business_once ON lead.pixel_receipts(connection_id,scope_key,event_name,business_key)
        WHERE business_key IS NOT NULL AND counted;
      CREATE INDEX pixel_report ON lead.pixel_receipts(tenant_id,agent_id,occurred_at,event_name) WHERE scope_key='live' AND counted;
      INSERT INTO lead.pixel_definitions(tenant_id,agent_id,connection_id,name,label,method,definition)
        SELECT c.tenant_id,c.agent_id,c.id,d.name,d.label,d.method,d.definition
        FROM lead.analytics_connections c CROSS JOIN (VALUES
          ('CompleteRegistration','注册成功','browser',''),
          ('Activation','有效使用','browser','首次成功完成核心行为'),
          ('Purchase','支付成功','server','')
        ) AS d(name,label,method,definition) WHERE c.provider='lead_site';
      DO $$ DECLARE item text; BEGIN
        FOREACH item IN ARRAY ARRAY['pixel_definitions','pixel_tests','pixel_touchpoints','pixel_identities','pixel_receipts'] LOOP
          EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',item);
          EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',item);
          EXECUTE format('CREATE POLICY agent_scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()))',item);
          EXECUTE format('CREATE POLICY member_agent_scope ON lead.%I AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id))',item);
        END LOOP;
      END $$;
    """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("Pixel source snapshots and event receipts must be retained")
