-- lead PostgreSQL 18 database baseline, 2026-10-29.
-- Apply to an empty development database through a reviewed migration.
-- This file defines schema and default-deny RLS; it does NOT provision runtime
-- database roles, grants, a production secret store, or a complete backend.
-- Agent tools never receive a SQL connection. Trusted services set transaction-
-- local app.tenant_id and app.agent_id after authentication and authorization.
BEGIN;
CREATE SCHEMA lead;

CREATE FUNCTION lead.ctx_tenant() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;
CREATE FUNCTION lead.ctx_agent() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.agent_id', true), '')::uuid
$$;

CREATE TABLE lead.tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz
);
CREATE TABLE lead.memberships (
  tenant_id uuid NOT NULL REFERENCES lead.tenants(id),
  oidc_issuer text NOT NULL, oidc_subject text NOT NULL,
  role text NOT NULL CHECK(role IN ('owner','operator','viewer')),
  status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,oidc_issuer,oidc_subject)
);
CREATE TABLE lead.agents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES lead.tenants(id), name text NOT NULL,
  desired_state text NOT NULL DEFAULT 'draft' CHECK(desired_state IN ('draft','running','paused','archived')),
  health_state text NOT NULL DEFAULT 'provisioning' CHECK(health_state IN ('provisioning','ready','working','queued','needs_auth','needs_attention','degraded','stopped')),
  control_version bigint NOT NULL DEFAULT 1 CHECK(control_version>0),
  config_revision integer NOT NULL DEFAULT 1 CHECK(config_revision>0),
  authorization_version integer NOT NULL DEFAULT 1 CHECK(authorization_version>0),
  onboarding_state jsonb NOT NULL DEFAULT '{"stage":"product"}',
  version bigint NOT NULL DEFAULT 1, heartbeat_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,id)
);
CREATE TABLE lead.agent_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  revision integer NOT NULL CHECK(revision>0), provider text NOT NULL,
  model_ref text NOT NULL, prompt_version text NOT NULL, toolset_version text NOT NULL,
  output_schema_version text NOT NULL, preferences jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,revision), UNIQUE(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
ALTER TABLE lead.agents ADD CONSTRAINT current_agent_revision
  FOREIGN KEY(tenant_id,id,config_revision)
  REFERENCES lead.agent_revisions(tenant_id,agent_id,revision)
  DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE lead.business_profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  revision integer NOT NULL, product_url text, description text NOT NULL DEFAULT '',
  facts jsonb NOT NULL DEFAULT '{}', source_refs jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,revision), UNIQUE(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  purpose text NOT NULL DEFAULT 'main', next_seq bigint NOT NULL DEFAULT 1,
  summary jsonb NOT NULL DEFAULT '{}', summary_through_seq bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,purpose),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.conversation_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  conversation_id uuid NOT NULL, seq bigint NOT NULL, client_message_id text,
  role text NOT NULL CHECK(role IN ('user','assistant','tool')),
  content jsonb NOT NULL, status text NOT NULL CHECK(status IN ('pending','complete','failed')),
  source_refs jsonb NOT NULL DEFAULT '[]', created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,conversation_id,id),
  UNIQUE(conversation_id,seq), UNIQUE(conversation_id,client_message_id),
  FOREIGN KEY(tenant_id,agent_id,conversation_id) REFERENCES lead.conversations(tenant_id,agent_id,id)
);
CREATE TABLE lead.conversation_commands (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  conversation_id uuid NOT NULL, message_id uuid NOT NULL, intent_index integer NOT NULL,
  command_type text NOT NULL, operation_key text NOT NULL,
  expected_revision integer, payload jsonb NOT NULL, result jsonb,
  state text NOT NULL CHECK(state IN ('proposed','accepted','running','succeeded','failed','needs_confirmation','canceled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,operation_key),
  UNIQUE(message_id,intent_index),
  FOREIGN KEY(tenant_id,agent_id,conversation_id,message_id)
    REFERENCES lead.conversation_messages(tenant_id,agent_id,conversation_id,id)
);
CREATE TABLE lead.network_identities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  provider_ref text NOT NULL, endpoint_ref text NOT NULL, secret_ref text NOT NULL,
  mode text NOT NULL CHECK(mode IN ('dedicated','shared_development')),
  expected_ip inet, expected_region text, policy_version integer NOT NULL,
  state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','verified','failed','retired')),
  verified_at timestamptz, valid_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
  CHECK(mode<>'dedicated' OR state<>'verified' OR expected_ip IS NOT NULL),
  CHECK(expected_ip IS NULL OR masklen(expected_ip)=CASE family(expected_ip) WHEN 4 THEN 32 ELSE 128 END)
);
CREATE UNIQUE INDEX dedicated_egress_unique ON lead.network_identities(expected_ip)
  WHERE mode='dedicated' AND state='verified' AND expected_ip IS NOT NULL;
CREATE TABLE lead.browser_profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  provider text NOT NULL, engine_version text NOT NULL, sdk_version text NOT NULL,
  config_ref text NOT NULL, config_hash text NOT NULL, storage_ref text NOT NULL,
  key_material_ref text NOT NULL, network_identity_id uuid NOT NULL,
  node_id text, os_uid integer, state text NOT NULL DEFAULT 'stopped',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id), UNIQUE(tenant_id,agent_id,id), UNIQUE(storage_ref),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
  FOREIGN KEY(tenant_id,agent_id,network_identity_id) REFERENCES lead.network_identities(tenant_id,agent_id,id)
);
CREATE UNIQUE INDEX profile_uid_unique ON lead.browser_profiles(node_id,os_uid)
  WHERE node_id IS NOT NULL AND os_uid IS NOT NULL;
CREATE TABLE lead.profile_leases (
  profile_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  owner_node_id text, owner_actor_id text, epoch bigint NOT NULL DEFAULT 0 CHECK(epoch>=0),
  control_version bigint NOT NULL, expires_at timestamptz, isolated_at timestamptz,
  state text NOT NULL DEFAULT 'released' CHECK(state IN ('released','held','uncertain','isolated')),
  FOREIGN KEY(tenant_id,agent_id,profile_id) REFERENCES lead.browser_profiles(tenant_id,agent_id,id)
);
CREATE TABLE lead.browser_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  profile_id uuid NOT NULL, node_id text NOT NULL, actor_id text NOT NULL,
  epoch bigint NOT NULL, control_version bigint NOT NULL,
  state text NOT NULL CHECK(state IN ('starting','ready','working','human','uncertain','closed')),
  heartbeat_at timestamptz, started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,profile_id,id),
  FOREIGN KEY(tenant_id,agent_id,profile_id) REFERENCES lead.browser_profiles(tenant_id,agent_id,id),
  CHECK((state='closed')=(ended_at IS NOT NULL))
);
CREATE UNIQUE INDEX one_live_browser ON lead.browser_sessions(profile_id) WHERE ended_at IS NULL;
CREATE TABLE lead.takeover_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  browser_session_id uuid NOT NULL, profile_id uuid NOT NULL, token_hash text NOT NULL UNIQUE,
  control_version bigint NOT NULL, state text NOT NULL CHECK(state IN ('requested','active','ending','ended','expired')),
  expires_at timestamptz NOT NULL, exchanged_at timestamptz, ended_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(tenant_id,agent_id,profile_id,browser_session_id) REFERENCES lead.browser_sessions(tenant_id,agent_id,profile_id,id)
);
CREATE UNIQUE INDEX one_live_takeover ON lead.takeover_sessions(profile_id)
  WHERE state IN ('requested','active','ending');
CREATE TABLE lead.identity_checks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  profile_id uuid NOT NULL, network_identity_id uuid NOT NULL,
  engine_version text NOT NULL, config_hash text NOT NULL, observed_ip inet,
  result text NOT NULL CHECK(result IN ('passed','failed','unknown')),
  checks jsonb NOT NULL, evidence_ref text, checked_at timestamptz NOT NULL DEFAULT now(), valid_until timestamptz,
  FOREIGN KEY(tenant_id,agent_id,profile_id) REFERENCES lead.browser_profiles(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id,network_identity_id) REFERENCES lead.network_identities(tenant_id,agent_id,id)
);
CREATE TABLE lead.platform_capabilities (
  platform text NOT NULL, action_type text NOT NULL, target_type text NOT NULL,
  technical_state text NOT NULL DEFAULT 'unverified' CHECK(technical_state IN ('unverified','verified','failed')),
  permission_state text NOT NULL DEFAULT 'pending' CHECK(permission_state IN ('pending','approved','restricted','disabled')),
  adapter_version text, reviewed_at timestamptz, evidence_ref text,
  PRIMARY KEY(platform,action_type,target_type)
);
CREATE TABLE lead.social_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  profile_id uuid NOT NULL, platform text NOT NULL, external_account_id text, display_name text,
  state text NOT NULL CHECK(state IN ('pending','connected','needs_auth','restricted','disconnected')),
  verified_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id,profile_id) REFERENCES lead.browser_profiles(tenant_id,agent_id,id),
  CHECK(state<>'connected' OR external_account_id IS NOT NULL)
);
CREATE UNIQUE INDEX one_account_controller ON lead.social_accounts(platform,external_account_id)
  WHERE state IN ('connected','needs_auth','restricted') AND external_account_id IS NOT NULL;
CREATE TABLE lead.account_targets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  account_id uuid NOT NULL, target_type text NOT NULL, external_target_id text NOT NULL,
  canonical_url text, display_name text NOT NULL, verified_permissions jsonb NOT NULL,
  state text NOT NULL CHECK(state IN ('active','disabled')),
  UNIQUE(tenant_id,agent_id,id), UNIQUE(account_id,target_type,external_target_id),
  FOREIGN KEY(tenant_id,agent_id,account_id) REFERENCES lead.social_accounts(tenant_id,agent_id,id)
);
CREATE TABLE lead.authorization_grants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  target_id uuid NOT NULL, version integer NOT NULL, allowed_actions jsonb NOT NULL,
  limits jsonb NOT NULL DEFAULT '{}', granted_by text NOT NULL,
  granted_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz,
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,target_id,version),
  UNIQUE(tenant_id,agent_id,target_id,id),
  FOREIGN KEY(tenant_id,agent_id,target_id) REFERENCES lead.account_targets(tenant_id,agent_id,id)
);
CREATE TABLE lead.assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  object_ref text NOT NULL UNIQUE, media_type text NOT NULL, size_bytes bigint NOT NULL CHECK(size_bytes>=0),
  sha256 text NOT NULL, rights jsonb NOT NULL, state text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id), FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.knowledge_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  revision integer NOT NULL, title text NOT NULL, source_type text NOT NULL, source_ref text,
  body_ref text NOT NULL, sha256 text NOT NULL, trust_state text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz,
  UNIQUE(tenant_id,agent_id,id), FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.knowledge_chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  document_id uuid NOT NULL, ordinal integer NOT NULL, body text NOT NULL,
  search_vector tsvector, embedding_ref text, embedding_version text,
  UNIQUE(tenant_id,agent_id,id), UNIQUE(document_id,ordinal),
  FOREIGN KEY(tenant_id,agent_id,document_id) REFERENCES lead.knowledge_documents(tenant_id,agent_id,id)
);
CREATE INDEX knowledge_search ON lead.knowledge_chunks USING gin(search_vector);
CREATE INDEX knowledge_scope ON lead.knowledge_chunks(tenant_id,agent_id,document_id);
CREATE TABLE lead.memory_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  kind text NOT NULL CHECK(kind IN ('preference','fact','strategy','observation')),
  body jsonb NOT NULL, source_refs jsonb NOT NULL, confirmation text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz, deleted_at timestamptz,
  UNIQUE(tenant_id,agent_id,id), FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  revision integer NOT NULL, agent_revision_id uuid NOT NULL, business_profile_id uuid NOT NULL,
  body jsonb NOT NULL, state text NOT NULL, valid_until timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,revision),
  FOREIGN KEY(tenant_id,agent_id,agent_revision_id) REFERENCES lead.agent_revisions(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id,business_profile_id) REFERENCES lead.business_profiles(tenant_id,agent_id,id)
);
CREATE TABLE lead.content_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  plan_id uuid NOT NULL, target_id uuid NOT NULL, revision integer NOT NULL,
  body jsonb NOT NULL, source_refs jsonb NOT NULL, validation_result jsonb NOT NULL,
  content_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,target_id,id),
  FOREIGN KEY(tenant_id,agent_id,plan_id) REFERENCES lead.plans(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id,target_id) REFERENCES lead.account_targets(tenant_id,agent_id,id)
);
CREATE TABLE lead.tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  plan_id uuid, type text NOT NULL, state text NOT NULL,
  workflow_id text NOT NULL UNIQUE, workflow_run_id text,
  input jsonb NOT NULL, result jsonb, scheduled_at timestamptz NOT NULL,
  started_at timestamptz, completed_at timestamptz, version bigint NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
  FOREIGN KEY(tenant_id,agent_id,plan_id) REFERENCES lead.plans(tenant_id,agent_id,id),
  CHECK(state IN ('planned','queued','running','waiting_user','reconciling','succeeded','failed','canceled'))
);
CREATE INDEX task_scope_time ON lead.tasks(tenant_id,agent_id,scheduled_at DESC);
CREATE TABLE lead.actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  task_id uuid NOT NULL, target_id uuid NOT NULL, content_id uuid, grant_id uuid NOT NULL,
  operation_key text NOT NULL, type text NOT NULL, intent jsonb NOT NULL,
  expected_control_version bigint NOT NULL, expected_config_revision integer NOT NULL,
  state text NOT NULL DEFAULT 'prepared' CHECK(state IN ('prepared','dispatching','confirmed','unknown','not_applied','canceled')),
  external_id text, external_url text, evidence_ref text,
  created_at timestamptz NOT NULL DEFAULT now(), confirmed_at timestamptz, version bigint NOT NULL DEFAULT 1,
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,operation_key),
  FOREIGN KEY(tenant_id,agent_id,task_id) REFERENCES lead.tasks(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id,target_id) REFERENCES lead.account_targets(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id,target_id,content_id) REFERENCES lead.content_items(tenant_id,agent_id,target_id,id),
  FOREIGN KEY(tenant_id,agent_id,target_id,grant_id) REFERENCES lead.authorization_grants(tenant_id,agent_id,target_id,id),
  CHECK(state<>'confirmed' OR (confirmed_at IS NOT NULL AND
    (external_id IS NOT NULL OR external_url IS NOT NULL OR evidence_ref IS NOT NULL)))
);
CREATE TABLE lead.action_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  action_id uuid NOT NULL, browser_session_id uuid NOT NULL, attempt integer NOT NULL,
  epoch bigint NOT NULL, control_version bigint NOT NULL, outcome text,
  observed_account_id text NOT NULL, observed_target_id text NOT NULL,
  evidence_ref text, error_code text, started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
  UNIQUE(action_id,attempt),
  FOREIGN KEY(tenant_id,agent_id,action_id) REFERENCES lead.actions(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id,browser_session_id) REFERENCES lead.browser_sessions(tenant_id,agent_id,id),
  CHECK(outcome IS NULL OR outcome IN ('confirmed','unknown','not_applied'))
);
CREATE FUNCTION lead.guard_action_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.operation_key,NEW.intent,NEW.target_id,NEW.content_id,NEW.grant_id)
     IS DISTINCT FROM ROW(OLD.operation_key,OLD.intent,OLD.target_id,OLD.content_id,OLD.grant_id) THEN
    RAISE EXCEPTION 'Action intent is immutable';
  END IF;
  IF NEW.state=OLD.state THEN RETURN NEW; END IF;
  IF (OLD.state='prepared' AND NEW.state IN ('dispatching','canceled')) OR
     (OLD.state='dispatching' AND NEW.state IN ('confirmed','unknown','not_applied')) OR
     (OLD.state='unknown' AND NEW.state IN ('confirmed','not_applied')) OR
     (OLD.state='not_applied' AND NEW.state IN ('prepared','canceled')) THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'Illegal action transition: % -> %', OLD.state, NEW.state;
END $$;
CREATE TRIGGER action_transition BEFORE UPDATE ON lead.actions
  FOR EACH ROW EXECUTE FUNCTION lead.guard_action_state();

CREATE TABLE lead.tracking_links (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  content_id uuid, platform text NOT NULL, public_code text NOT NULL UNIQUE,
  destination_url text NOT NULL, allowed_host text NOT NULL, state text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
  FOREIGN KEY(tenant_id,agent_id,content_id) REFERENCES lead.content_items(tenant_id,agent_id,id)
);
CREATE TABLE lead.analytics_connections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  provider text NOT NULL, site_host text NOT NULL, secret_ref text NOT NULL,
  verification_ref text, state text NOT NULL CHECK(state IN ('pending','verified','failed','disconnected')), last_received_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id), FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.visit_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  tracking_link_id uuid, source text NOT NULL, source_session_id text NOT NULL,
  trust_level text NOT NULL CHECK(trust_level IN ('redirect_observed','client_observed','server_verified')),
  qualified boolean, exclusion_reason text, rule_version text NOT NULL,
  started_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,source,source_session_id),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
  FOREIGN KEY(tenant_id,agent_id,tracking_link_id) REFERENCES lead.tracking_links(tenant_id,agent_id,id)
);
CREATE TABLE lead.conversion_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  connection_id uuid NOT NULL, visit_session_id uuid, source_event_id text NOT NULL,
  type text NOT NULL, value numeric(20,6), currency char(3),
  trust_level text NOT NULL CHECK(trust_level IN ('client_observed','server_verified')), rule_version text NOT NULL, occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,connection_id,source_event_id),
  FOREIGN KEY(tenant_id,agent_id,connection_id) REFERENCES lead.analytics_connections(tenant_id,agent_id,id),
  FOREIGN KEY(tenant_id,agent_id,visit_session_id) REFERENCES lead.visit_sessions(tenant_id,agent_id,id)
);
CREATE TABLE lead.daily_metrics (
  tenant_id uuid NOT NULL, agent_id uuid NOT NULL, day date NOT NULL,
  platform text NOT NULL, measurement_version text NOT NULL,
  clicks bigint NOT NULL DEFAULT 0, visits bigint, qualified_visits bigint, conversions bigint,
  coverage jsonb NOT NULL, calculated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(tenant_id,agent_id,day,platform,measurement_version),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.budget_periods (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL, unit text NOT NULL,
  limit_units bigint NOT NULL CHECK(limit_units>=0), reserved_units bigint NOT NULL DEFAULT 0 CHECK(reserved_units>=0),
  spent_units bigint NOT NULL DEFAULT 0 CHECK(spent_units>=0),
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,starts_at,unit),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id), CHECK(ends_at>starts_at)
);
CREATE TABLE lead.budget_reservations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  period_id uuid NOT NULL, operation_key text NOT NULL, units bigint NOT NULL CHECK(units>=0),
  state text NOT NULL CHECK(state IN ('reserved','settled','released','unknown')),
  expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,operation_key),
  FOREIGN KEY(tenant_id,agent_id,period_id) REFERENCES lead.budget_periods(tenant_id,agent_id,id)
);
CREATE TABLE lead.usage_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
  reservation_id uuid NOT NULL, event_key text NOT NULL, provider_ref text,
  category text NOT NULL, units bigint NOT NULL CHECK(units>=0), rate_version text NOT NULL,
  occurred_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,agent_id,event_key),
  FOREIGN KEY(tenant_id,agent_id,reservation_id) REFERENCES lead.budget_reservations(tenant_id,agent_id,id)
);
CREATE TABLE lead.idempotency_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES lead.tenants(id), agent_id uuid,
  actor_ref text NOT NULL, scope text NOT NULL, idempotency_key text NOT NULL,
  request_hash text NOT NULL, state text NOT NULL CHECK(state IN ('pending','complete','failed')),
  response_ref text, http_status integer, created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
  UNIQUE(tenant_id,actor_ref,scope,idempotency_key),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.outbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid,
  aggregate_type text NOT NULL, aggregate_id uuid NOT NULL, aggregate_version bigint NOT NULL,
  event_type text NOT NULL, schema_version integer NOT NULL, payload jsonb NOT NULL,
  trace_id text NOT NULL, occurred_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  FOREIGN KEY(tenant_id) REFERENCES lead.tenants(id),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE INDEX pending_outbox ON lead.outbox_events(occurred_at) WHERE published_at IS NULL;
CREATE TABLE lead.inbox_events (
  tenant_id uuid NOT NULL REFERENCES lead.tenants(id), agent_id uuid,
  consumer text NOT NULL, event_id uuid NOT NULL, processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(consumer,event_id),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE TABLE lead.audit_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES lead.tenants(id), agent_id uuid,
  actor_type text NOT NULL, actor_ref text NOT NULL, type text NOT NULL,
  object_ref text NOT NULL, payload jsonb NOT NULL, trace_id text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
);
CREATE INDEX audit_scope_time ON lead.audit_events(tenant_id,agent_id,occurred_at DESC);

-- A trusted API transaction uses app.agent_id='' for an authorized tenant-wide
-- view. Agent execution transactions MUST bind a non-empty, authorized Agent ID.
ALTER TABLE lead.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lead.tenants USING(id=lead.ctx_tenant()) WITH CHECK(id=lead.ctx_tenant());
ALTER TABLE lead.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lead.memberships USING(tenant_id=lead.ctx_tenant()) WITH CHECK(tenant_id=lead.ctx_tenant());
ALTER TABLE lead.agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.agents FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_scope ON lead.agents
  USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR id=lead.ctx_agent()))
  WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR id=lead.ctx_agent()));
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'agent_revisions','business_profiles','conversations','conversation_messages','conversation_commands',
    'network_identities','browser_profiles','profile_leases','browser_sessions','takeover_sessions','identity_checks',
    'social_accounts','account_targets','authorization_grants','assets','knowledge_documents','knowledge_chunks',
    'memory_entries','plans','content_items','tasks','actions','action_attempts','tracking_links',
    'analytics_connections','visit_sessions','conversion_events','daily_metrics','budget_periods',
    'budget_reservations','usage_events','outbox_events','inbox_events','audit_events','idempotency_requests'
  ] LOOP
    EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',t);
    EXECUTE format('CREATE POLICY agent_scope ON lead.%I USING (tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK (tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()))',t);
  END LOOP;
END $$;

-- Roles/grants are a separate reviewed infrastructure migration:
-- runtime roles are NO BYPASSRLS and not table owners; no TRUNCATE/DDL privileges.
-- A narrowly scoped identity resolver bootstraps OIDC membership authorization.
-- A dedicated outbox publisher receives only its required metadata permissions
-- and an explicit role-specific policy, not unrestricted tenant business data.
-- Audit writers are INSERT-only. Actual provider cost may exceed estimates;
-- settlement records it and pauses future work instead of discarding the cost.
COMMIT;
