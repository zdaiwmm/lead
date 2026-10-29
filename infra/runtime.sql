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
DO $$ DECLARE t text; BEGIN
FOREACH t IN ARRAY ARRAY['activity','chat_jobs','lab_posts','browser_runs','browser_steps','interaction_cards'] LOOP
 EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',t);
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='lead' AND tablename=t AND policyname='agent_scope') THEN
 EXECUTE format('CREATE POLICY agent_scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()))',t);
 END IF;
END LOOP; END $$;
CREATE INDEX IF NOT EXISTS activity_scope_time ON lead.activity(tenant_id,agent_id,created_at DESC);
INSERT INTO lead.schema_versions(version) VALUES(1) ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION lead.confirm_telegram_probe(p_action uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE target_uuid uuid;
BEGIN
 SELECT a.target_id INTO target_uuid FROM lead.actions a
 JOIN lead.tasks t ON t.id=a.task_id AND t.tenant_id=a.tenant_id AND t.agent_id=a.agent_id
 JOIN lead.account_targets at ON at.id=a.target_id AND at.tenant_id=a.tenant_id AND at.agent_id=a.agent_id
 JOIN lead.social_accounts s ON s.id=at.account_id
 JOIN lead.interaction_cards card ON card.id::text=t.input->>'key' AND card.tenant_id=a.tenant_id AND card.agent_id=a.agent_id
 WHERE a.id=p_action AND a.tenant_id=lead.ctx_tenant() AND a.agent_id=lead.ctx_agent()
 AND a.state='confirmed' AND a.evidence_ref IS NOT NULL AND a.external_id IS NOT NULL
 AND t.input->>'bootstrap_verification'='true' AND s.platform='telegram' AND s.state='connected'
 AND card.state='accepted' AND card.type='verify_publish'
 AND card.payload->>'target_id'=a.target_id::text AND card.payload->>'grant_id'=a.grant_id::text;
 IF target_uuid IS NULL THEN RETURN false; END IF;
 UPDATE lead.account_targets SET verified_permissions=verified_permissions || '{"publishing_verified":true}'::jsonb WHERE id=target_uuid;
 UPDATE lead.platform_capabilities SET technical_state='verified',adapter_version='telegram-webk-1',reviewed_at=now(),evidence_ref=p_action::text
 WHERE platform='telegram' AND action_type='publish' AND target_type='channel' AND permission_state='approved';
 RETURN true;
END $$;
