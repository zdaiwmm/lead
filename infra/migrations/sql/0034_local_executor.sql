-- Local Codex transports carry execution, never social-account authority.
CREATE TABLE lead.local_executor_devices (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 name text NOT NULL DEFAULT '', platform text NOT NULL DEFAULT '', state text NOT NULL DEFAULT 'pairing'
   CHECK(state IN ('pairing','active','revoked')),
 pairing_hash text, pairing_expires_at timestamptz, token_hash text,
 epoch bigint NOT NULL DEFAULT 1 CHECK(epoch>0), instance_id text,
 capabilities jsonb NOT NULL DEFAULT '{}', last_seen_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz,
 UNIQUE(tenant_id,agent_id,id),
 FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.local_executor_projects (
 tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 mode text NOT NULL DEFAULT 'legacy' CHECK(mode IN ('legacy','codex')), device_id uuid,
 updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,agent_id),
 FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
 FOREIGN KEY(tenant_id,agent_id,device_id) REFERENCES lead.local_executor_devices(tenant_id,agent_id,id)
);
CREATE TABLE lead.local_executor_requests (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 device_id uuid, task_id uuid, request_key text NOT NULL, request_hash text NOT NULL, session_key text NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN
   ('queued','running','waiting_input','completed','failed','interrupted','execution_unknown','canceled')),
 payload jsonb NOT NULL, scope jsonb NOT NULL DEFAULT '{}', result jsonb, usage jsonb,
 thread_id text, turn_id text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
 UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,request_key),
 FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
 FOREIGN KEY(tenant_id,agent_id,device_id) REFERENCES lead.local_executor_devices(tenant_id,agent_id,id),
 FOREIGN KEY(tenant_id,agent_id,task_id) REFERENCES lead.tasks(tenant_id,agent_id,id)
);
CREATE INDEX local_executor_session ON lead.local_executor_requests(tenant_id,agent_id,session_key,created_at);
CREATE TABLE lead.local_executor_commands (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 request_id uuid NOT NULL, command_key text NOT NULL, command_hash text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('run','steer','interrupt','respond')), payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), acknowledged_at timestamptz,
 UNIQUE(tenant_id,agent_id,request_id,command_key),
 FOREIGN KEY(tenant_id,agent_id,request_id) REFERENCES lead.local_executor_requests(tenant_id,agent_id,id)
);
CREATE TABLE lead.local_executor_events (
 tenant_id uuid NOT NULL, agent_id uuid NOT NULL, request_id uuid NOT NULL,
 event_id text NOT NULL, event_hash text NOT NULL, method text NOT NULL, params jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,agent_id,event_id),
 FOREIGN KEY(tenant_id,agent_id,request_id) REFERENCES lead.local_executor_requests(tenant_id,agent_id,id)
);
DO $$ DECLARE tab text; BEGIN
 FOREACH tab IN ARRAY ARRAY['local_executor_devices','local_executor_projects','local_executor_requests',
   'local_executor_commands','local_executor_events'] LOOP
   EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',tab);
   EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',tab);
   EXECUTE format('CREATE POLICY agent_scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()))',tab);
   EXECUTE format('CREATE POLICY member_scope ON lead.%I AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id))',tab);
 END LOOP;
END $$;
