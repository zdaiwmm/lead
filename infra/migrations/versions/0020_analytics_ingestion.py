"""Scoped durable analytics admission, dispatch leases and clean event facts."""

from alembic import op

revision = "0020_analytics_ingestion"
down_revision = "0019_operating_progress"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE lead.outbox_events
          ADD COLUMN delivery_attempts integer NOT NULL DEFAULT 0,
          ADD COLUMN available_at timestamptz NOT NULL DEFAULT now(),
          ADD COLUMN lease_token uuid,
          ADD COLUMN lease_until timestamptz,
          ADD COLUMN delivery_error text,
          ADD COLUMN dispatch_generation integer NOT NULL DEFAULT 0;
        ALTER TABLE lead.outbox_events ADD UNIQUE(tenant_id,agent_id,id);

        CREATE TABLE lead.analytics_ingest_events (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL,
          agent_id uuid NOT NULL,
          scope_key text NOT NULL,
          environment text GENERATED ALWAYS AS
            (CASE WHEN scope_key='live' THEN 'production' ELSE 'test' END) STORED,
          connection_id uuid,
          source_kind text NOT NULL CHECK(source_kind IN ('browser','server','redirect')),
          event_id text NOT NULL CHECK(length(event_id) BETWEEN 1 AND 128),
          event_name text NOT NULL,
          payload_sha256 text NOT NULL CHECK(length(payload_sha256)=64),
          snapshot_sha256 text NOT NULL CHECK(length(snapshot_sha256)=64),
          processing_version text NOT NULL DEFAULT 'analytics-clean-v1',
          state text NOT NULL CHECK(state IN ('pending','processed','quarantined','failed')),
          received_at timestamptz NOT NULL,
          last_seen_at timestamptz NOT NULL,
          delivery_attempts integer NOT NULL DEFAULT 1,
          processing_attempts integer NOT NULL DEFAULT 0,
          cycle_attempts integer NOT NULL DEFAULT 0,
          next_attempt_at timestamptz NOT NULL DEFAULT now(),
          lease_token uuid,
          lease_until timestamptz,
          error_code text,
          error_detail text,
          outbox_id uuid,
          payload_expires_at timestamptz NOT NULL,
          tombstone_until timestamptz NOT NULL,
          UNIQUE(tenant_id,agent_id,id),
          UNIQUE(tenant_id,agent_id,scope_key,id),
          UNIQUE(tenant_id,agent_id,scope_key,event_id),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
          FOREIGN KEY(tenant_id,agent_id,connection_id)
            REFERENCES lead.analytics_connections(tenant_id,agent_id,id),
          FOREIGN KEY(tenant_id,agent_id,outbox_id)
            REFERENCES lead.outbox_events(tenant_id,agent_id,id)
            DEFERRABLE INITIALLY DEFERRED,
          CHECK(source_kind='redirect' OR connection_id IS NOT NULL),
          CHECK(payload_expires_at<=tombstone_until),
          CHECK(scope_key='live' OR scope_key ~ '^[0-9a-f-]{36}$')
        );
        CREATE INDEX analytics_ingest_pending ON lead.analytics_ingest_events
          (tenant_id,agent_id,scope_key,next_attempt_at,received_at)
          WHERE state='pending';
        CREATE INDEX analytics_ingest_diagnostics ON lead.analytics_ingest_events
          (tenant_id,agent_id,scope_key,state,received_at DESC);
        CREATE INDEX analytics_dispatch_pending ON lead.outbox_events
          (tenant_id,agent_id,(payload->>'scope_key'),available_at,occurred_at)
          WHERE event_type='analytics.ingest' AND published_at IS NULL;

        CREATE TABLE lead.analytics_event_payloads (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,
          ingest_id uuid NOT NULL,payload jsonb NOT NULL,snapshot jsonb NOT NULL,
          storage_sha256 text NOT NULL CHECK(length(storage_sha256)=64),
          expires_at timestamptz NOT NULL,
          PRIMARY KEY(tenant_id,agent_id,scope_key,ingest_id),
          FOREIGN KEY(tenant_id,agent_id,scope_key,ingest_id)
            REFERENCES lead.analytics_ingest_events(tenant_id,agent_id,scope_key,id),
          CHECK(jsonb_typeof(payload)='object' AND jsonb_typeof(snapshot)='object')
        );
        CREATE TABLE lead.analytics_event_network (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,
          ingest_id uuid NOT NULL,client_ip inet,user_agent text NOT NULL,
          expires_at timestamptz NOT NULL,
          PRIMARY KEY(tenant_id,agent_id,scope_key,ingest_id),
          FOREIGN KEY(tenant_id,agent_id,scope_key,ingest_id)
            REFERENCES lead.analytics_ingest_events(tenant_id,agent_id,scope_key,id)
        );
        CREATE INDEX analytics_network_expiry ON lead.analytics_event_network
          (tenant_id,agent_id,scope_key,expires_at);

        CREATE TABLE lead.analytics_clean_events (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,
          ingest_id uuid NOT NULL,event_id text NOT NULL,event_name text NOT NULL,
          connection_id uuid,occurred_at timestamptz NOT NULL,received_at timestamptz NOT NULL,
          anonymous_id uuid,session_id uuid,user_key text,conversion_id text,
          properties jsonb NOT NULL,context jsonb NOT NULL,device jsonb NOT NULL,
          country_code text,quality_flags text[] NOT NULL DEFAULT '{}',
          processing_version text NOT NULL,result jsonb NOT NULL,
          expires_at timestamptz NOT NULL,
          PRIMARY KEY(tenant_id,agent_id,scope_key,ingest_id),
          UNIQUE(tenant_id,agent_id,scope_key,event_id),
          FOREIGN KEY(tenant_id,agent_id,scope_key,ingest_id)
            REFERENCES lead.analytics_ingest_events(tenant_id,agent_id,scope_key,id),
          FOREIGN KEY(tenant_id,agent_id,connection_id)
            REFERENCES lead.analytics_connections(tenant_id,agent_id,id)
        );
        CREATE INDEX analytics_clean_time ON lead.analytics_clean_events
          (tenant_id,agent_id,scope_key,occurred_at DESC,ingest_id);
        CREATE INDEX analytics_clean_event ON lead.analytics_clean_events
          (tenant_id,agent_id,scope_key,event_name,occurred_at DESC);
        CREATE INDEX analytics_clean_visitor ON lead.analytics_clean_events
          (tenant_id,agent_id,scope_key,anonymous_id,occurred_at) WHERE anonymous_id IS NOT NULL;

        CREATE FUNCTION lead.analytics_scope_matches(t uuid,a uuid,s text)
        RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
          SELECT t=lead.ctx_tenant() AND a=lead.ctx_agent()
            AND s=nullif(current_setting('app.analytics_scope',true),'')
        $$;
        DO $$ DECLARE item text; BEGIN
          FOREACH item IN ARRAY ARRAY['analytics_ingest_events','analytics_event_payloads',
            'analytics_event_network','analytics_clean_events'] LOOP
            EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',item);
            EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',item);
            EXECUTE format('CREATE POLICY analytics_scope ON lead.%I USING(lead.analytics_scope_matches(tenant_id,agent_id,scope_key)) WITH CHECK(lead.analytics_scope_matches(tenant_id,agent_id,scope_key))',item);
            EXECUTE format('CREATE POLICY member_agent_scope ON lead.%I AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id))',item);
          END LOOP;
        END $$;
        CREATE POLICY analytics_dispatch_scope ON lead.outbox_events AS RESTRICTIVE
          USING(event_type<>'analytics.ingest' OR
            (agent_id=lead.ctx_agent() AND payload->>'scope_key'=nullif(current_setting('app.analytics_scope',true),'')))
          WITH CHECK(event_type<>'analytics.ingest' OR
            (agent_id=lead.ctx_agent() AND payload->>'scope_key'=nullif(current_setting('app.analytics_scope',true),'')));

        CREATE FUNCTION lead.analytics_processing_payload(i uuid,l uuid)
        RETURNS TABLE(payload jsonb,snapshot jsonb,storage_sha256 text)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
          SELECT p.payload,p.snapshot,p.storage_sha256
          FROM lead.analytics_event_payloads p JOIN lead.analytics_ingest_events e
            ON (e.tenant_id,e.agent_id,e.scope_key,e.id)=
               (p.tenant_id,p.agent_id,p.scope_key,p.ingest_id)
          WHERE e.id=i AND e.state='pending' AND e.lease_token=l AND e.lease_until>now()
            AND p.expires_at>now()
            AND lead.analytics_scope_matches(e.tenant_id,e.agent_id,e.scope_key)
        $$;
        CREATE FUNCTION lead.analytics_processing_network(i uuid,l uuid)
        RETURNS TABLE(client_ip inet,user_agent text)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
          SELECT p.client_ip,p.user_agent
          FROM lead.analytics_event_network p JOIN lead.analytics_ingest_events e
            ON (e.tenant_id,e.agent_id,e.scope_key,e.id)=
               (p.tenant_id,p.agent_id,p.scope_key,p.ingest_id)
          WHERE e.id=i AND e.state='pending' AND e.lease_token=l AND e.lease_until>now()
            AND p.expires_at>now()
            AND lead.analytics_scope_matches(e.tenant_id,e.agent_id,e.scope_key)
        $$;
        REVOKE EXECUTE ON FUNCTION lead.analytics_processing_payload(uuid,uuid),
          lead.analytics_processing_network(uuid,uuid) FROM PUBLIC;

        CREATE FUNCTION lead.analytics_immutable_identity() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
          IF (to_jsonb(NEW)->'tenant_id',to_jsonb(NEW)->'agent_id',to_jsonb(NEW)->'scope_key')
             IS DISTINCT FROM
             (to_jsonb(OLD)->'tenant_id',to_jsonb(OLD)->'agent_id',to_jsonb(OLD)->'scope_key') THEN
            RAISE EXCEPTION 'analytics scope is immutable' USING ERRCODE='23514';
          END IF;
          IF TG_TABLE_NAME='analytics_ingest_events' THEN
            IF (NEW.id,NEW.source_kind,NEW.connection_id,NEW.event_name,NEW.outbox_id,
                NEW.event_id,NEW.payload_sha256,NEW.snapshot_sha256,NEW.received_at,NEW.processing_version)
               IS DISTINCT FROM
               (OLD.id,OLD.source_kind,OLD.connection_id,OLD.event_name,OLD.outbox_id,
                OLD.event_id,OLD.payload_sha256,OLD.snapshot_sha256,OLD.received_at,OLD.processing_version) THEN
              RAISE EXCEPTION 'accepted event identity is immutable' USING ERRCODE='23514';
            END IF;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER analytics_ingest_immutable BEFORE UPDATE ON lead.analytics_ingest_events
          FOR EACH ROW EXECUTE FUNCTION lead.analytics_immutable_identity();
        CREATE TRIGGER analytics_clean_immutable BEFORE UPDATE ON lead.analytics_clean_events
          FOR EACH ROW EXECUTE FUNCTION lead.analytics_immutable_identity();
        """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError(
        "Durable analytics receipts and accepted events must be retained"
    )
