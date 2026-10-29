CREATE TABLE IF NOT EXISTS lead.schema_versions(version integer PRIMARY KEY, applied_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS lead.activity(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 title text NOT NULL, detail text NOT NULL DEFAULT '', level text NOT NULL DEFAULT 'info', task_id uuid,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS lead.chat_jobs(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 message_id uuid NOT NULL, status text NOT NULL DEFAULT 'pending', error_code text,
 created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
 UNIQUE(message_id), FOREIGN KEY(tenant_id,agent_id,message_id) REFERENCES lead.conversation_messages(tenant_id,agent_id,id)
);
ALTER TABLE lead.chat_jobs ADD COLUMN IF NOT EXISTS error_context jsonb;
CREATE TABLE IF NOT EXISTS lead.lab_posts(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,agent_id uuid NOT NULL,
 operation_key text NOT NULL, body text NOT NULL, created_at timestamptz DEFAULT now(),
 UNIQUE(agent_id,operation_key), FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS lead.browser_runs(
 id uuid PRIMARY KEY,tenant_id uuid NOT NULL,agent_id uuid NOT NULL,
 message_id uuid,task_id uuid,goal text NOT NULL,mode text NOT NULL DEFAULT 'research',
 control_version bigint NOT NULL,config_revision integer NOT NULL,
 state text NOT NULL DEFAULT 'running' CHECK(state IN ('running','succeeded','failed','canceled','needs_user')),
 error_code text,started_at timestamptz NOT NULL DEFAULT now(),ended_at timestamptz,
 UNIQUE(tenant_id,agent_id,id),
 FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
 FOREIGN KEY(tenant_id,agent_id,message_id) REFERENCES lead.conversation_messages(tenant_id,agent_id,id),
 FOREIGN KEY(tenant_id,agent_id,task_id) REFERENCES lead.tasks(tenant_id,agent_id,id)
);
CREATE TABLE IF NOT EXISTS lead.browser_steps(
 id uuid PRIMARY KEY,tenant_id uuid NOT NULL,agent_id uuid NOT NULL,run_id uuid NOT NULL,
 seq integer NOT NULL,command_id uuid NOT NULL UNIQUE,operation text NOT NULL,input jsonb NOT NULL,
 state text NOT NULL CHECK(state IN ('started','completed','failed')),
 output jsonb,screenshot_ref text,error_code text,
 started_at timestamptz NOT NULL DEFAULT now(),ended_at timestamptz,
 UNIQUE(run_id,seq),UNIQUE(tenant_id,agent_id,id),
 FOREIGN KEY(tenant_id,agent_id,run_id) REFERENCES lead.browser_runs(tenant_id,agent_id,id)
);
CREATE TABLE IF NOT EXISTS lead.interaction_cards(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,agent_id uuid NOT NULL,
 type text NOT NULL,payload jsonb NOT NULL,version integer NOT NULL DEFAULT 1,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','accepted','canceled','expired')),
 created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL,
 UNIQUE(tenant_id,agent_id,id),FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
ALTER TABLE lead.action_attempts ADD COLUMN IF NOT EXISTS observation jsonb;
ALTER TABLE lead.assets ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS lead.publish_preparations(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,agent_id uuid NOT NULL,action_id uuid NOT NULL,
 stage text NOT NULL CHECK(stage IN ('preparing','ready','uncertain','submitted')),manifest jsonb NOT NULL,
 observation jsonb NOT NULL DEFAULT '{}',created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(action_id),FOREIGN KEY(tenant_id,agent_id,action_id) REFERENCES lead.actions(tenant_id,agent_id,id)
);
DO $$ DECLARE t text; BEGIN
FOREACH t IN ARRAY ARRAY['activity','chat_jobs','lab_posts','browser_runs','browser_steps','interaction_cards','publish_preparations'] LOOP
 EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',t);
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='lead' AND tablename=t AND policyname='agent_scope') THEN
 EXECUTE format('CREATE POLICY agent_scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()))',t);
 END IF;
END LOOP; END $$;
CREATE INDEX IF NOT EXISTS activity_scope_time ON lead.activity(tenant_id,agent_id,created_at DESC);
INSERT INTO lead.schema_versions(version) VALUES(1) ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION lead.confirm_channel_probe(p_action uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE target_uuid uuid; channel text; target_kind text; release text; format text; defaults jsonb;
BEGIN
 SELECT a.target_id,s.platform,at.target_type,a.intent->>'adapter_version',coalesce(a.intent->>'format','text'),jsonb_build_object('made_for_kids',card.payload->'made_for_kids','commercial_disclosure',card.payload->'commercial_disclosure')
 INTO target_uuid,channel,target_kind,release,format,defaults
 FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id AND t.tenant_id=a.tenant_id AND t.agent_id=a.agent_id
 JOIN lead.account_targets at ON at.id=a.target_id AND at.tenant_id=a.tenant_id AND at.agent_id=a.agent_id
 JOIN lead.social_accounts s ON s.id=at.account_id
 JOIN lead.authorization_grants g ON g.id=a.grant_id AND g.target_id=a.target_id AND g.revoked_at IS NULL
 JOIN lead.interaction_cards card ON card.id::text=t.input->>'key' AND card.tenant_id=a.tenant_id AND card.agent_id=a.agent_id
 JOIN lead.platform_capabilities pc ON pc.platform=s.platform AND pc.action_type='publish' AND pc.target_type=at.target_type AND pc.permission_state='approved'
 WHERE a.id=p_action AND a.tenant_id=lead.ctx_tenant() AND a.agent_id=lead.ctx_agent()
 AND a.state='confirmed' AND a.evidence_ref IS NOT NULL AND a.external_id IS NOT NULL
 AND t.type='connection.verify_publish' AND t.input->>'bootstrap_verification'='true' AND s.state='connected'
 AND card.state='accepted' AND card.type='verify_publish'
 AND card.payload->>'target_id'=a.target_id::text AND card.payload->>'grant_id'=a.grant_id::text
 AND card.payload->>'body'=a.intent->>'body'
 AND coalesce(card.payload->'media','[]'::jsonb)=coalesce(a.intent->'media','[]'::jsonb)
 AND card.payload->'title' IS NOT DISTINCT FROM a.intent->'title'
 AND card.payload->'made_for_kids' IS NOT DISTINCT FROM a.intent->'made_for_kids'
 AND card.payload->'commercial_disclosure' IS NOT DISTINCT FROM a.intent->'commercial_disclosure'
 AND coalesce(card.payload->>'format','text')=coalesce(a.intent->>'format','text')
 AND a.intent->>'adapter_version'=pc.adapter_version AND a.intent->>'adapter_version'=at.verified_permissions->>'adapter_version'
 AND EXISTS(SELECT 1 FROM lead.action_attempts ap WHERE ap.action_id=a.id AND ap.outcome='confirmed' AND ap.observed_account_id=s.external_account_id AND ap.observed_target_id=at.external_target_id);
 IF target_uuid IS NULL OR release IS NULL THEN RETURN false; END IF;
 UPDATE lead.account_targets SET verified_permissions=verified_permissions || jsonb_build_object('publication_defaults',coalesce(verified_permissions->'publication_defaults','{}'::jsonb)||jsonb_strip_nulls(defaults),'publishing_verified',true,'publishing_verified_formats',(SELECT jsonb_agg(v) FROM (SELECT DISTINCT jsonb_array_elements(coalesce(verified_permissions->'publishing_verified_formats','[]'::jsonb)||jsonb_build_array(format)) AS v) x)) WHERE id=target_uuid;
 UPDATE lead.platform_capabilities SET technical_state='verified',adapter_version=release,reviewed_at=now(),evidence_ref=p_action::text WHERE platform=channel AND action_type='publish' AND target_type=target_kind;
 RETURN true;
END $$;
CREATE OR REPLACE FUNCTION lead.confirm_telegram_probe(p_action uuid) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$ SELECT lead.confirm_channel_probe(p_action) $$;
ALTER TABLE lead.plans ADD COLUMN IF NOT EXISTS cycle_key text;
CREATE UNIQUE INDEX IF NOT EXISTS plan_cycle_once ON lead.plans(tenant_id,agent_id,cycle_key) WHERE cycle_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS lead.analytics_credentials(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid NOT NULL,agent_id uuid NOT NULL,connection_id uuid NOT NULL,
 secret_sha256 text NOT NULL UNIQUE,state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')),
 created_at timestamptz NOT NULL DEFAULT now(),last_received_at timestamptz,revoked_at timestamptz,
 UNIQUE(tenant_id,agent_id,id),FOREIGN KEY(tenant_id,agent_id,connection_id) REFERENCES lead.analytics_connections(tenant_id,agent_id,id)
);
ALTER TABLE lead.analytics_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.analytics_credentials FORCE ROW LEVEL SECURITY;
DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='lead' AND tablename='analytics_credentials' AND policyname='agent_scope') THEN
CREATE POLICY agent_scope ON lead.analytics_credentials USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())); END IF; END $$;
ALTER TABLE lead.conversion_events ADD COLUMN IF NOT EXISTS payload_sha256 text;
