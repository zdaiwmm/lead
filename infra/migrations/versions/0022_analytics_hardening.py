"""Agent admission budgets, signed click lineage and bounded retention tiers."""

from alembic import op

revision = "0022_analytics_hardening"
down_revision = "0021_analytics_reporting"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
        CREATE TABLE lead.analytics_policies (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,
          events_per_minute integer NOT NULL DEFAULT 6000 CHECK(events_per_minute BETWEEN 1 AND 1000000),
          event_burst integer NOT NULL DEFAULT 200 CHECK(event_burst BETWEEN 20 AND 100000),
          requests_per_minute integer NOT NULL DEFAULT 30000 CHECK(requests_per_minute BETWEEN 1 AND 1000000),
          request_burst integer NOT NULL DEFAULT 500 CHECK(request_burst BETWEEN 1 AND 100000),
          ip_events_per_minute integer NOT NULL DEFAULT 600 CHECK(ip_events_per_minute BETWEEN 0 AND 1000000),
          ip_event_burst integer NOT NULL DEFAULT 40 CHECK(ip_event_burst BETWEEN 20 AND 100000),
          daily_events bigint NOT NULL DEFAULT 0 CHECK(daily_events>=0),
          redirect_per_minute integer NOT NULL DEFAULT 6000 CHECK(redirect_per_minute BETWEEN 1 AND 1000000),
          redirect_burst integer NOT NULL DEFAULT 200 CHECK(redirect_burst BETWEEN 1 AND 100000),
          network_days integer NOT NULL DEFAULT 7 CHECK(network_days BETWEEN 1 AND 30),
          payload_days integer NOT NULL DEFAULT 30 CHECK(payload_days BETWEEN 1 AND 90),
          clean_days integer NOT NULL DEFAULT 90 CHECK(clean_days BETWEEN 30 AND 365),
          backlog_seconds integer NOT NULL DEFAULT 300 CHECK(backlog_seconds BETWEEN 10 AND 86400),
          delivery_failure_threshold integer NOT NULL DEFAULT 5 CHECK(delivery_failure_threshold BETWEEN 1 AND 1000),
          revision bigint NOT NULL DEFAULT 1,updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,agent_id),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
          CHECK(network_days<=payload_days AND payload_days<=clean_days)
        );
        CREATE TABLE lead.analytics_limit_buckets (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,environment text NOT NULL CHECK(environment IN ('production','test')),
          kind text NOT NULL,actor_key text NOT NULL,tokens numeric NOT NULL,updated_at timestamptz NOT NULL,
          expires_at timestamptz NOT NULL,
          PRIMARY KEY(tenant_id,agent_id,environment,kind,actor_key),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
        );
        CREATE INDEX analytics_bucket_expiry ON lead.analytics_limit_buckets(tenant_id,agent_id,expires_at);
        CREATE TABLE lead.analytics_limit_counters (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,environment text NOT NULL CHECK(environment IN ('production','test')),
          day date NOT NULL,granted_requests bigint NOT NULL DEFAULT 0,granted_events bigint NOT NULL DEFAULT 0,
          denied_requests bigint NOT NULL DEFAULT 0,denied_events bigint NOT NULL DEFAULT 0,
          skipped_redirects bigint NOT NULL DEFAULT 0,last_denied_at timestamptz,
          PRIMARY KEY(tenant_id,agent_id,environment,day),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
        );
        DO $$ DECLARE item text; BEGIN
          FOREACH item IN ARRAY ARRAY['analytics_policies','analytics_limit_buckets','analytics_limit_counters'] LOOP
            EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',item);
            EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',item);
            EXECUTE format('CREATE POLICY analytics_agent_scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent()) WITH CHECK(tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent())',item);
            EXECUTE format('CREATE POLICY member_agent_scope ON lead.%I AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id))',item);
          END LOOP;
        END $$;
        CREATE TABLE lead.analytics_clicks (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL DEFAULT 'live' CHECK(scope_key='live'),
          id uuid NOT NULL,tracking_link_id uuid NOT NULL,destination_host text NOT NULL,
          occurred_at timestamptz NOT NULL,expires_at timestamptz NOT NULL,
          attribution_policy jsonb NOT NULL,attribution_version text NOT NULL,
          PRIMARY KEY(tenant_id,agent_id,id),
          UNIQUE(tenant_id,agent_id,scope_key,id),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
          FOREIGN KEY(tenant_id,agent_id,tracking_link_id) REFERENCES lead.tracking_links(tenant_id,agent_id,id)
        );
        ALTER TABLE lead.pixel_touchpoints ADD COLUMN click_id uuid,
          ADD FOREIGN KEY(tenant_id,agent_id,click_id) REFERENCES lead.analytics_clicks(tenant_id,agent_id,id);
        CREATE UNIQUE INDEX analytics_click_touch ON lead.pixel_touchpoints(connection_id,anonymous_id,click_id) WHERE click_id IS NOT NULL;
        ALTER TABLE lead.pixel_receipts ADD COLUMN click_id uuid,
          ADD FOREIGN KEY(tenant_id,agent_id,scope_key,click_id) REFERENCES lead.analytics_clicks(tenant_id,agent_id,scope_key,id);
        ALTER TABLE lead.analytics_clean_events ADD COLUMN click_id uuid,
          ADD FOREIGN KEY(tenant_id,agent_id,scope_key,click_id) REFERENCES lead.analytics_clicks(tenant_id,agent_id,scope_key,id);
        ALTER TABLE lead.analytics_ingest_events ADD COLUMN fingerprint_version smallint NOT NULL DEFAULT 1,
          ADD COLUMN anonymous_id uuid,ADD COLUMN session_id uuid;
        ALTER TABLE lead.analytics_ingest_events ADD CHECK(fingerprint_version IN (1,2));
        ALTER TABLE lead.analytics_event_payloads ADD COLUMN privacy_version smallint NOT NULL DEFAULT 0;
        ALTER TABLE lead.analytics_clean_events ADD COLUMN privacy_version smallint NOT NULL DEFAULT 0;
        CREATE FUNCTION lead.analytics_receipt_identity() RETURNS trigger
          LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            IF (NEW.fingerprint_version,NEW.anonymous_id,NEW.session_id) IS DISTINCT FROM
               (OLD.fingerprint_version,OLD.anonymous_id,OLD.session_id) THEN
              RAISE EXCEPTION 'accepted receipt binding is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
          END $$;
        CREATE TRIGGER analytics_receipt_binding BEFORE UPDATE ON lead.analytics_ingest_events
          FOR EACH ROW EXECUTE FUNCTION lead.analytics_receipt_identity();

        CREATE TABLE lead.analytics_event_archives (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,
          ingest_id uuid NOT NULL,event_id text NOT NULL,connection_id uuid,click_id uuid,
          occurred_at timestamptz NOT NULL,event jsonb NOT NULL,archived_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,agent_id,scope_key,ingest_id),
          UNIQUE(tenant_id,agent_id,scope_key,event_id),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
          FOREIGN KEY(tenant_id,agent_id,connection_id) REFERENCES lead.analytics_connections(tenant_id,agent_id,id),
          FOREIGN KEY(tenant_id,agent_id,scope_key,click_id) REFERENCES lead.analytics_clicks(tenant_id,agent_id,scope_key,id),
          CHECK(event->>'tenant_id'=tenant_id::text AND event->>'agent_id'=agent_id::text AND event->>'scope_key'=scope_key)
        );
        CREATE INDEX analytics_archives_time ON lead.analytics_event_archives(tenant_id,agent_id,scope_key,occurred_at);
        CREATE TABLE lead.analytics_receipt_tombstones (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,event_id text NOT NULL,
          id uuid NOT NULL,connection_id uuid,source_kind text NOT NULL,event_name text NOT NULL,
          payload_sha256 text NOT NULL,fingerprint_version smallint NOT NULL,
          state text NOT NULL,error_code text,received_at timestamptz NOT NULL,
          anonymous_id uuid,session_id uuid,result jsonb,archived_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,agent_id,scope_key,event_id),UNIQUE(tenant_id,agent_id,scope_key,id),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
          FOREIGN KEY(tenant_id,agent_id,connection_id) REFERENCES lead.analytics_connections(tenant_id,agent_id,id)
        );
        DO $$ DECLARE item text; BEGIN
          FOREACH item IN ARRAY ARRAY['analytics_clicks','analytics_event_archives','analytics_receipt_tombstones'] LOOP
            EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',item);
            EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',item);
            EXECUTE format('CREATE POLICY analytics_scope ON lead.%I USING(lead.analytics_scope_matches(tenant_id,agent_id,scope_key)) WITH CHECK(lead.analytics_scope_matches(tenant_id,agent_id,scope_key))',item);
            EXECUTE format('CREATE POLICY member_agent_scope ON lead.%I AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id))',item);
          END LOOP;
        END $$;
        CREATE INDEX analytics_payload_expiry ON lead.analytics_event_payloads(tenant_id,agent_id,scope_key,expires_at);
        CREATE INDEX analytics_clean_expiry ON lead.analytics_clean_events(tenant_id,agent_id,scope_key,expires_at);
        CREATE INDEX analytics_receipt_expiry ON lead.analytics_ingest_events(tenant_id,agent_id,scope_key,tombstone_until);
        CREATE INDEX analytics_click_expiry ON lead.analytics_clicks(tenant_id,agent_id,expires_at);
        CREATE INDEX analytics_receipt_click ON lead.pixel_receipts(tenant_id,agent_id,click_id) WHERE click_id IS NOT NULL;
        CREATE TRIGGER analytics_click_scope_immutable BEFORE UPDATE ON lead.analytics_clicks
          FOR EACH ROW EXECUTE FUNCTION lead.analytics_immutable_identity();
        CREATE TRIGGER analytics_archive_scope_immutable BEFORE UPDATE ON lead.analytics_event_archives
          FOR EACH ROW EXECUTE FUNCTION lead.analytics_immutable_identity();
        CREATE TRIGGER analytics_tombstone_scope_immutable BEFORE UPDATE ON lead.analytics_receipt_tombstones
          FOR EACH ROW EXECUTE FUNCTION lead.analytics_immutable_identity();
        """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("Keep click evidence, archived facts and deduplication receipts")
