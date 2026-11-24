CREATE TABLE lead.google_opportunity_policies (
  tenant_id uuid NOT NULL,
  agent_id uuid PRIMARY KEY,
  profile_id uuid,
  email_digest text,
  account_hint text,
  state text NOT NULL DEFAULT 'unbound' CHECK(state IN ('unbound','ready','needs_owner')),
  enabled boolean NOT NULL DEFAULT false,
  allow_free_signup boolean NOT NULL DEFAULT false,
  revision bigint NOT NULL DEFAULT 1,
  FOREIGN KEY (tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
  FOREIGN KEY (tenant_id,agent_id,profile_id) REFERENCES lead.browser_profiles(tenant_id,agent_id,id),
  CHECK((profile_id IS NULL)=(email_digest IS NULL)),
  CHECK((profile_id IS NULL)=(state='unbound'))
);
CREATE TABLE lead.google_opportunity_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  agent_id uuid NOT NULL,
  profile_id uuid NOT NULL,
  task_id uuid NOT NULL,
  origin text NOT NULL,
  revision bigint NOT NULL,
  status text NOT NULL,
  reason text,
  dispatched boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (profile_id,origin),
  FOREIGN KEY (tenant_id,agent_id,profile_id) REFERENCES lead.browser_profiles(tenant_id,agent_id,id),
  FOREIGN KEY (tenant_id,agent_id,task_id) REFERENCES lead.tasks(tenant_id,agent_id,id)
);
DO $$
DECLARE tab text;
BEGIN
  FOREACH tab IN ARRAY ARRAY['google_opportunity_policies','google_opportunity_attempts'] LOOP
    EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',tab);
    EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',tab);
    EXECUTE format('CREATE POLICY agent_scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent() AND lead.member_agent_allowed(agent_id)) WITH CHECK(tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent() AND lead.member_agent_allowed(agent_id))',tab);
  END LOOP;
END $$;
