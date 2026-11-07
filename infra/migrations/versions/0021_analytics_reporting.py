"""Scoped hourly/daily reporting snapshots and exact audience membership."""

from alembic import op

revision = "0021_analytics_reporting"
down_revision = "0020_analytics_ingestion"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE lead.analytics_ingest_events ADD COLUMN processed_at timestamptz;
        ALTER TABLE lead.agents ADD COLUMN analytics_revision bigint NOT NULL DEFAULT 0;
        CREATE TABLE lead.analytics_report_dirty (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,
          day date NOT NULL,revision bigint NOT NULL DEFAULT 1,request_token uuid NOT NULL DEFAULT gen_random_uuid(),
          changed_at timestamptz NOT NULL DEFAULT now(),
          state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','failed')),
          attempts integer NOT NULL DEFAULT 0,next_attempt_at timestamptz NOT NULL DEFAULT now(),error_code text,
          PRIMARY KEY(tenant_id,agent_id,scope_key,day),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
        );
        CREATE TABLE lead.analytics_report_days (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,day date NOT NULL,
          revision bigint NOT NULL DEFAULT 1,refreshed_at timestamptz NOT NULL,
          source_rows bigint NOT NULL,version text NOT NULL,
          PRIMARY KEY(tenant_id,agent_id,scope_key,day),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
        );
        CREATE TABLE lead.analytics_rollups (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,
          grain text NOT NULL CHECK(grain IN ('day','hour')),day date NOT NULL,bucket timestamptz NOT NULL,
          channel text NOT NULL,event_name text NOT NULL,currency text NOT NULL DEFAULT '',
          observed bigint NOT NULL,counted bigint NOT NULL,server_counted bigint NOT NULL,
          clicks bigint NOT NULL,pv bigint NOT NULL,sessions bigint NOT NULL,
          attributed_sessions bigint NOT NULL,qualified_visits bigint NOT NULL,
          unknown_pv bigint NOT NULL,unknown_visitors bigint NOT NULL,unknown_users bigint NOT NULL,
          server_revenue numeric(30,6) NOT NULL,
          PRIMARY KEY(tenant_id,agent_id,scope_key,grain,bucket,channel,event_name,currency),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
        );
        CREATE INDEX analytics_rollups_range ON lead.analytics_rollups
          (tenant_id,agent_id,scope_key,grain,day,channel,event_name);
        CREATE TABLE lead.analytics_audience (
          tenant_id uuid NOT NULL,agent_id uuid NOT NULL,scope_key text NOT NULL,
          grain text NOT NULL CHECK(grain IN ('day','hour')),day date NOT NULL,bucket timestamptz NOT NULL,
          channel text NOT NULL,kind text NOT NULL,identity_key text NOT NULL,
          PRIMARY KEY(tenant_id,agent_id,scope_key,grain,bucket,channel,kind,identity_key),
          FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
        );
        CREATE INDEX analytics_audience_range ON lead.analytics_audience
          (tenant_id,agent_id,scope_key,grain,day,kind,channel,identity_key);
        DO $$ DECLARE item text; BEGIN
          FOREACH item IN ARRAY ARRAY['analytics_report_dirty','analytics_report_days','analytics_rollups','analytics_audience'] LOOP
            EXECUTE format('ALTER TABLE lead.%I ADD COLUMN test_session_id uuid GENERATED ALWAYS AS (CASE WHEN scope_key=''live'' THEN NULL ELSE scope_key::uuid END) STORED',item);
            EXECUTE format('ALTER TABLE lead.%I ADD FOREIGN KEY(tenant_id,agent_id,test_session_id) REFERENCES lead.pixel_tests(tenant_id,agent_id,id)',item);
            EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',item);
            EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',item);
            EXECUTE format('CREATE POLICY analytics_scope ON lead.%I USING(lead.analytics_scope_matches(tenant_id,agent_id,scope_key)) WITH CHECK(lead.analytics_scope_matches(tenant_id,agent_id,scope_key))',item);
            EXECUTE format('CREATE POLICY member_agent_scope ON lead.%I AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id))',item);
          END LOOP;
        END $$;
        CREATE INDEX analytics_report_visits ON lead.visit_sessions(tenant_id,agent_id,started_at);
        CREATE INDEX analytics_report_receipts ON lead.pixel_receipts(tenant_id,agent_id,scope_key,occurred_at);
        CREATE INDEX analytics_report_conversions ON lead.conversion_events(tenant_id,agent_id,occurred_at);

        CREATE FUNCTION lead.analytics_mark_report_dirty(t uuid,a uuid,s text,at_time timestamptz)
        RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        DECLARE previous_scope text:=current_setting('app.analytics_scope',true);
        BEGIN
          PERFORM set_config('app.analytics_scope',s,true);
          INSERT INTO lead.analytics_report_dirty(tenant_id,agent_id,scope_key,day)
            VALUES(t,a,s,(at_time AT TIME ZONE 'Asia/Shanghai')::date)
            ON CONFLICT(tenant_id,agent_id,scope_key,day) DO UPDATE SET
              revision=lead.analytics_report_dirty.revision+1,changed_at=clock_timestamp();
          PERFORM set_config('app.analytics_scope',coalesce(previous_scope,''),true);
        END $$;
        CREATE FUNCTION lead.analytics_report_change() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        DECLARE s text:='live'; at_time timestamptz; old_time timestamptz;
        BEGIN
          IF TG_TABLE_NAME='pixel_receipts' THEN
            s:=NEW.scope_key;at_time:=NEW.occurred_at;
            IF TG_OP='UPDATE' THEN old_time:=OLD.occurred_at; END IF;
          ELSIF TG_TABLE_NAME='visit_sessions' THEN
            IF NEW.source LIKE 'pixel-test:%%' THEN s:=substring(NEW.source from 12); END IF;
            at_time:=NEW.started_at;
            IF TG_OP='UPDATE' THEN
              old_time:=OLD.started_at;
              IF OLD.source IS DISTINCT FROM NEW.source THEN
                PERFORM lead.analytics_mark_report_dirty(OLD.tenant_id,OLD.agent_id,
                  CASE WHEN OLD.source LIKE 'pixel-test:%%' THEN substring(OLD.source from 12) ELSE 'live' END,OLD.started_at);
              END IF;
            END IF;
          ELSE at_time:=NEW.occurred_at;
            IF TG_OP='UPDATE' THEN old_time:=OLD.occurred_at; END IF;
          END IF;
          PERFORM lead.analytics_mark_report_dirty(NEW.tenant_id,NEW.agent_id,s,at_time);
          IF old_time IS NOT NULL AND (old_time AT TIME ZONE 'Asia/Shanghai')::date<>(at_time AT TIME ZONE 'Asia/Shanghai')::date THEN
            PERFORM lead.analytics_mark_report_dirty(NEW.tenant_id,NEW.agent_id,s,old_time);
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER analytics_report_pixel AFTER INSERT OR UPDATE ON lead.pixel_receipts
          FOR EACH ROW EXECUTE FUNCTION lead.analytics_report_change();
        CREATE TRIGGER analytics_report_visit AFTER INSERT OR UPDATE ON lead.visit_sessions
          FOR EACH ROW EXECUTE FUNCTION lead.analytics_report_change();
        CREATE TRIGGER analytics_report_conversion AFTER INSERT OR UPDATE ON lead.conversion_events
          FOR EACH ROW EXECUTE FUNCTION lead.analytics_report_change();
        INSERT INTO lead.analytics_report_dirty(tenant_id,agent_id,scope_key,day)
          SELECT tenant_id,agent_id,scope_key,(occurred_at AT TIME ZONE 'Asia/Shanghai')::date FROM lead.pixel_receipts
          UNION SELECT tenant_id,agent_id,CASE WHEN source LIKE 'pixel-test:%%' THEN substring(source from 12) ELSE 'live' END,
            (started_at AT TIME ZONE 'Asia/Shanghai')::date FROM lead.visit_sessions
          UNION SELECT tenant_id,agent_id,'live',(occurred_at AT TIME ZONE 'Asia/Shanghai')::date FROM lead.conversion_events;
        """,
        execution_options={"no_parameters": True},
    )


def downgrade():
    raise RuntimeError("Preserve reporting snapshots and rebuild history")
