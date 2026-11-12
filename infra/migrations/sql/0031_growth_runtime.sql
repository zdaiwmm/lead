CREATE TABLE lead.growth_experiments (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 revision bigint NOT NULL DEFAULT 1, state text NOT NULL DEFAULT 'active'
 CHECK(state IN ('active','observing','closed','superseded')),
 business_profile_id uuid, objective_revision bigint NOT NULL DEFAULT 1,
 question_key text NOT NULL, variation_key text NOT NULL,
 specification jsonb NOT NULL, conclusion jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz,
 UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,question_key,objective_revision,variation_key),
 FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
 FOREIGN KEY(tenant_id,agent_id,business_profile_id) REFERENCES lead.business_profiles(tenant_id,agent_id,id)
);
ALTER TABLE lead.tasks ADD COLUMN experiment_id uuid;
ALTER TABLE lead.tasks ADD CONSTRAINT task_growth_experiment FOREIGN KEY(tenant_id,agent_id,experiment_id)
 REFERENCES lead.growth_experiments(tenant_id,agent_id,id);
CREATE INDEX growth_task_due ON lead.tasks(tenant_id,agent_id,scheduled_at,id)
 WHERE type LIKE 'growth.%' AND state IN ('planned','queued','running','reconciling','waiting_user');
-- A reusable draft has its own lifecycle; it does not require a calendar or account.
ALTER TABLE lead.content_items ALTER COLUMN plan_id DROP NOT NULL;
ALTER TABLE lead.content_items ALTER COLUMN target_id DROP NOT NULL;
ALTER TABLE lead.actions DROP CONSTRAINT actions_tenant_id_agent_id_target_id_content_id_fkey;
ALTER TABLE lead.actions ADD CONSTRAINT action_scoped_content FOREIGN KEY(tenant_id,agent_id,content_id)
 REFERENCES lead.content_items(tenant_id,agent_id,id);
CREATE FUNCTION lead.growth_content_target() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.content_id IS NOT NULL AND NOT EXISTS(SELECT FROM lead.content_items c WHERE c.id=NEW.content_id
  AND c.tenant_id=NEW.tenant_id AND c.agent_id=NEW.agent_id AND (c.target_id IS NULL OR c.target_id=NEW.target_id))
 THEN RAISE EXCEPTION 'content_target_mismatch'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER content_target_before_action BEFORE INSERT ON lead.actions FOR EACH ROW EXECUTE FUNCTION lead.growth_content_target();
ALTER TABLE lead.social_accounts ADD COLUMN subject_ref jsonb;
ALTER TABLE lead.takeover_sessions ADD COLUMN purpose text NOT NULL DEFAULT 'manual'
 CHECK(purpose IN ('manual','login','challenge'));
ALTER TABLE lead.takeover_sessions ADD COLUMN input_seq bigint NOT NULL DEFAULT 0;
ALTER TABLE lead.takeover_sessions ADD COLUMN input_inflight boolean NOT NULL DEFAULT false;
ALTER TABLE lead.takeover_sessions ADD COLUMN composing boolean NOT NULL DEFAULT false;
ALTER TABLE lead.takeover_sessions ADD COLUMN last_input_at timestamptz;
ALTER TABLE lead.takeover_sessions ADD COLUMN completed_account_id uuid;
ALTER TABLE lead.takeover_sessions ADD CONSTRAINT login_completed_account FOREIGN KEY(tenant_id,agent_id,completed_account_id)
 REFERENCES lead.social_accounts(tenant_id,agent_id,id);

CREATE TABLE lead.account_operating_intents (
 account_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 revision bigint NOT NULL DEFAULT 1, desired text NOT NULL CHECK(desired IN ('running','paused','revoked')),
 source_kind text NOT NULL CHECK(source_kind IN ('connection','owner_command','verified_migration')),
 source_event_ref text NOT NULL, scope jsonb NOT NULL DEFAULT '{}', updated_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,agent_id,account_id) REFERENCES lead.social_accounts(tenant_id,agent_id,id),
 UNIQUE(tenant_id,agent_id,account_id)
);
CREATE TABLE lead.profile_execution_routes (
 profile_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 generation bigint NOT NULL DEFAULT 1, executor_ref text NOT NULL, transport_ref text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,agent_id,profile_id) REFERENCES lead.browser_profiles(tenant_id,agent_id,id),
 UNIQUE(tenant_id,agent_id,profile_id)
);
ALTER TABLE lead.browser_runs ADD COLUMN profile_id uuid;
ALTER TABLE lead.browser_runs ADD CONSTRAINT growth_research_profile FOREIGN KEY(tenant_id,agent_id,profile_id)
 REFERENCES lead.browser_profiles(tenant_id,agent_id,id);
ALTER TABLE lead.growth_experiments ADD COLUMN recovery_checked_at timestamptz NOT NULL DEFAULT '1970-01-01T00:00:00Z';
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['growth_experiments','account_operating_intents','profile_execution_routes'] LOOP
  EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY agent_scope ON lead.%I USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()))',t);
  EXECUTE format('CREATE POLICY member_scope ON lead.%I AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id))',t);
 END LOOP;
END $$;

CREATE FUNCTION lead.growth_read_intent(p_account uuid) RETURNS SETOF lead.account_operating_intents
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT * FROM lead.account_operating_intents WHERE account_id=p_account AND tenant_id=lead.ctx_tenant()
  AND agent_id=lead.ctx_agent() AND lead.member_agent_allowed(agent_id) FOR SHARE;
$$;
CREATE FUNCTION lead.growth_read_route(p_profile uuid) RETURNS SETOF lead.profile_execution_routes
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT * FROM lead.profile_execution_routes WHERE profile_id=p_profile AND tenant_id=lead.ctx_tenant()
  AND agent_id=lead.ctx_agent() AND lead.member_agent_allowed(agent_id) FOR SHARE;
$$;
REVOKE ALL ON FUNCTION lead.growth_read_intent(uuid),lead.growth_read_route(uuid) FROM PUBLIC;

CREATE FUNCTION lead.growth_set_intent(p_agent uuid,p_account uuid,p_desired text,p_source text,
 p_ref text,p_expected bigint,p_scope jsonb) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE a lead.social_accounts; old lead.account_operating_intents; n bigint;
BEGIN
 SELECT * INTO a FROM lead.social_accounts WHERE id=p_account AND agent_id=p_agent
  AND tenant_id=lead.ctx_tenant() FOR UPDATE;
 IF a.id IS NULL OR NOT lead.member_agent_allowed(p_agent) THEN RAISE EXCEPTION 'account_scope'; END IF;
 IF p_desired NOT IN ('running','paused','revoked') OR p_source NOT IN ('connection','owner_command','verified_migration')
  OR length(p_ref)<3 THEN RAISE EXCEPTION 'intent_source'; END IF;
 IF p_source='connection' AND NOT EXISTS(SELECT 1 FROM lead.takeover_sessions t
   WHERE t.agent_id=p_agent AND t.tenant_id=a.tenant_id AND t.purpose='login'
    AND t.completed_account_id=p_account AND p_ref='login-complete:'||t.id::text)
 THEN RAISE EXCEPTION 'connection_source_unverified'; END IF;
 SELECT * INTO old FROM lead.account_operating_intents WHERE account_id=p_account FOR UPDATE;
 IF old.account_id IS NOT NULL AND old.source_event_ref=p_ref THEN RETURN old.revision; END IF;
 IF p_expected IS NOT NULL AND coalesce(old.revision,0)<>p_expected THEN RAISE EXCEPTION 'intent_revision'; END IF;
 IF p_source='connection' AND old.account_id IS NOT NULL THEN RETURN old.revision; END IF;
 INSERT INTO lead.account_operating_intents(tenant_id,agent_id,account_id,desired,source_kind,source_event_ref,scope)
 VALUES(a.tenant_id,p_agent,p_account,p_desired,p_source,p_ref,coalesce(p_scope,old.scope,'{}'))
 ON CONFLICT(account_id) DO UPDATE SET revision=lead.account_operating_intents.revision+1,
 desired=EXCLUDED.desired,source_kind=EXCLUDED.source_kind,source_event_ref=EXCLUDED.source_event_ref,
 scope=EXCLUDED.scope,updated_at=now() RETURNING revision INTO n;
 RETURN n;
END $$;
REVOKE ALL ON FUNCTION lead.growth_set_intent(uuid,uuid,text,text,text,bigint,jsonb) FROM PUBLIC;

CREATE FUNCTION lead.growth_disconnect_intent() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
BEGIN
 IF NEW.state='disconnected' AND OLD.state IS DISTINCT FROM NEW.state THEN
  UPDATE lead.account_operating_intents SET desired='revoked',revision=revision+1,
   source_event_ref='account-disconnected:'||NEW.id::text||':'||NEW.binding_version::text,updated_at=now()
   WHERE account_id=NEW.id AND agent_id=NEW.agent_id AND tenant_id=NEW.tenant_id AND desired<>'revoked';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER growth_disconnect_intent AFTER UPDATE OF state ON lead.social_accounts
 FOR EACH ROW EXECUTE FUNCTION lead.growth_disconnect_intent();

CREATE FUNCTION lead.growth_complete_connection(p_agent uuid,p_takeover uuid,p_task uuid,p_input_seq bigint,
 p_route_generation bigint,p_binding bigint,p_subject jsonb,p_display text,p_evidence jsonb) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE takeover lead.takeover_sessions; work lead.tasks; account lead.social_accounts; connected uuid; intent_revision bigint;
BEGIN
 -- A trusted observer invokes this function; runtime and model tools have no EXECUTE privilege.
 IF NOT lead.member_agent_allowed(p_agent) OR p_subject->>'external_id' IS NULL
  OR p_subject->>'id_kind' NOT IN ('stable','handle') OR jsonb_array_length(p_evidence)=0
 THEN RAISE EXCEPTION 'connection_proof'; END IF;
 SELECT * INTO takeover FROM lead.takeover_sessions WHERE id=p_takeover AND agent_id=p_agent
  AND tenant_id=lead.ctx_tenant() FOR UPDATE;
 IF takeover.completed_account_id IS NOT NULL THEN RETURN takeover.completed_account_id; END IF;
 SELECT * INTO work FROM lead.tasks WHERE id=p_task AND agent_id=p_agent AND tenant_id=lead.ctx_tenant();
 IF work.type<>'growth.account' OR work.input#>>'{spec,payload,takeover_id}'<>p_takeover::text
  OR takeover.id IS NULL OR takeover.purpose<>'login' OR takeover.state<>'active' OR takeover.expires_at<=now()
  OR takeover.input_seq<>p_input_seq OR takeover.input_inflight OR takeover.composing
  OR takeover.last_input_at>now()-interval '2 seconds'
  OR NOT EXISTS(SELECT 1 FROM lead.browser_sessions b WHERE b.id=takeover.browser_session_id AND b.ended_at IS NULL
    AND b.profile_id=takeover.profile_id)
  OR NOT EXISTS(SELECT 1 FROM lead.profile_execution_routes r WHERE r.profile_id=takeover.profile_id AND r.generation=p_route_generation)
 THEN RAISE EXCEPTION 'connection_changed'; END IF;
 SELECT * INTO account FROM lead.social_accounts WHERE id=(work.input#>>'{spec,payload,connection_account_id}')::uuid
  AND agent_id=p_agent AND tenant_id=lead.ctx_tenant() AND profile_id=takeover.profile_id FOR UPDATE;
 IF account.id IS NULL OR account.binding_version<>p_binding OR account.platform<>p_subject->>'namespace'
 THEN RAISE EXCEPTION 'account_changed'; END IF;
 connected:=account.id;
 IF account.external_account_id IS NOT NULL AND account.external_account_id<>p_subject->>'external_id'
  AND NOT (p_subject->>'id_kind'='handle' AND lower(account.external_account_id)=lower(p_subject->>'external_id')) THEN
  IF p_subject->>'id_kind'='handle' THEN RAISE EXCEPTION 'connection_identity_ambiguous'; END IF;
  -- A new subject never inherits the former subject's grants or exclusions.
  UPDATE lead.social_accounts SET state='disconnected',automation_enabled=false,binding_version=binding_version+1 WHERE id=account.id;
  UPDATE lead.authorization_grants SET revoked_at=now() WHERE target_id IN(SELECT id FROM lead.account_targets WHERE account_id=account.id) AND revoked_at IS NULL;
  UPDATE lead.account_targets SET state='disabled' WHERE account_id=account.id;
  UPDATE lead.actions SET state='canceled' WHERE state='prepared' AND target_id IN(SELECT id FROM lead.account_targets WHERE account_id=account.id);
  INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,automation_enabled)
   VALUES(account.tenant_id,p_agent,account.profile_id,account.platform,'pending',false) RETURNING id INTO connected;
 END IF;
 UPDATE lead.social_accounts SET state='connected',external_account_id=p_subject->>'external_id',subject_ref=p_subject,
  display_name=p_display,verified_at=now(),binding_version=binding_version+CASE WHEN external_account_id IS NULL THEN 1 ELSE 0 END
  WHERE id=connected;
 UPDATE lead.takeover_sessions SET completed_account_id=connected WHERE id=p_takeover;
 intent_revision:=lead.growth_set_intent(p_agent,connected,'running','connection','login-complete:'||p_takeover::text,NULL,
  '{"target_scope":"verified_owned_targets","excluded_targets":[]}'::jsonb);
 UPDATE lead.social_accounts s SET automation_enabled=(i.desired='running') FROM lead.account_operating_intents i
  WHERE s.id=connected AND i.account_id=s.id;
 UPDATE lead.takeover_sessions SET state='ended',ended_at=now(),completed_account_id=connected WHERE id=p_takeover;
 INSERT INTO lead.audit_events(tenant_id,agent_id,actor_type,actor_ref,type,object_ref,payload,trace_id)
 VALUES(account.tenant_id,p_agent,'service','account_observer','account.connection_completed',p_takeover::text,
  jsonb_build_object('account_id',connected,'subject_ref',p_subject,'evidence',p_evidence,'intent_revision',intent_revision),'growth-connection');
 INSERT INTO lead.outbox_events(tenant_id,agent_id,aggregate_type,aggregate_id,aggregate_version,event_type,schema_version,payload,trace_id)
 VALUES(account.tenant_id,p_agent,'account',connected,intent_revision,'growth.account_connected',1,
  jsonb_build_object('reason','account_connected','account_id',connected,'takeover_id',p_takeover),'growth-connection');
 RETURN connected;
END $$;
REVOKE ALL ON FUNCTION lead.growth_complete_connection(uuid,uuid,uuid,bigint,bigint,bigint,jsonb,text,jsonb) FROM PUBLIC;

CREATE FUNCTION lead.growth_receipt_valid(p_action uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT EXISTS(SELECT FROM lead.actions a JOIN lead.tasks w ON w.id=a.task_id
  JOIN lead.content_items content ON content.id=a.content_id
  JOIN lead.account_targets t ON t.id=a.target_id JOIN lead.social_accounts s ON s.id=t.account_id
  JOIN lead.authorization_grants g ON g.id=a.grant_id AND g.target_id=t.id
  WHERE a.id=p_action AND a.tenant_id=lead.ctx_tenant() AND a.agent_id=lead.ctx_agent()
   AND lead.member_agent_allowed(a.agent_id) AND w.type='growth.channel' AND a.state='confirmed'
   AND a.external_id IS NOT NULL AND a.intent->>'body'=content.body->>'body'
   AND coalesce(a.intent->>'content_hash',content.content_hash)=content.content_hash
   AND coalesce(a.intent->'media','[]')=coalesce(content.body->'media','[]')
   AND ((s.platform='lab' AND EXISTS(SELECT FROM lead.lab_posts l WHERE l.agent_id=a.agent_id AND l.operation_key=a.operation_key))
    OR (a.evidence_ref IS NOT NULL AND EXISTS(SELECT FROM lead.action_attempts ap WHERE ap.action_id=a.id
      AND ap.outcome='confirmed' AND ap.observed_account_id=s.external_account_id
      AND ap.observed_target_id=t.external_target_id AND ap.control_version=a.expected_control_version
      AND ap.started_at>=g.granted_at AND (g.revoked_at IS NULL OR ap.started_at<g.revoked_at)))));
$$;
REVOKE ALL ON FUNCTION lead.growth_receipt_valid(uuid) FROM PUBLIC;

CREATE FUNCTION lead.growth_confirm_publication(p_action uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE target_uuid uuid; channel text; kind text; release text;
BEGIN
 IF NOT lead.growth_receipt_valid(p_action) THEN RETURN false; END IF;
 SELECT a.target_id,s.platform,coalesce(a.intent->>'format','text'),a.intent->>'adapter_version'
 INTO target_uuid,channel,kind,release
 FROM lead.actions a JOIN lead.tasks w ON w.id=a.task_id AND w.tenant_id=a.tenant_id AND w.agent_id=a.agent_id
 JOIN lead.content_items content ON content.id=a.content_id AND content.tenant_id=a.tenant_id AND content.agent_id=a.agent_id
 JOIN lead.account_targets t ON t.id=a.target_id AND t.tenant_id=a.tenant_id AND t.agent_id=a.agent_id
 JOIN lead.social_accounts s ON s.id=t.account_id AND s.tenant_id=a.tenant_id AND s.agent_id=a.agent_id
 JOIN lead.authorization_grants g ON g.id=a.grant_id AND g.target_id=t.id AND g.tenant_id=a.tenant_id AND g.agent_id=a.agent_id
 JOIN lead.platform_capabilities pc ON pc.platform=s.platform AND pc.action_type='publish' AND pc.target_type=t.target_type
 WHERE a.id=p_action AND a.tenant_id=lead.ctx_tenant() AND a.agent_id=lead.ctx_agent() AND lead.member_agent_allowed(a.agent_id)
 AND w.type='growth.channel' AND a.intent->>'growth_runtime'='1'
 AND a.intent->>'publication_purpose' IN ('business','diagnostic') AND a.intent->>'verification_required'='true'
 AND a.state='confirmed' AND a.external_id IS NOT NULL AND a.evidence_ref IS NOT NULL
 AND s.state='connected' AND t.state='active' AND s.binding_version=(a.intent->>'binding_version')::bigint
 AND a.intent->>'content_hash'=content.content_hash AND a.intent->>'body'=content.body->>'body'
 AND coalesce(a.intent->'media','[]')=coalesce(content.body->'media','[]')
 AND coalesce(a.intent->>'title','')=coalesce(content.body->>'title','')
 AND a.intent->>'adapter_version'=pc.adapter_version AND a.intent->>'adapter_version'=t.verified_permissions->>'adapter_version'
 AND (pc.permission_state='approved' OR (pc.permission_state='pending' AND lead.owner_browser_pilot_allowed(t.id,g.id)))
 AND EXISTS(SELECT 1 FROM lead.action_attempts ap WHERE ap.action_id=a.id AND ap.outcome='confirmed'
   AND ap.observed_account_id=s.external_account_id AND ap.observed_target_id=t.external_target_id
   AND ap.control_version=a.expected_control_version AND ap.started_at>=g.granted_at
   AND (g.revoked_at IS NULL OR ap.started_at<g.revoked_at));
 IF target_uuid IS NULL THEN RETURN false; END IF;
 UPDATE lead.account_targets SET verified_permissions=verified_permissions || jsonb_build_object(
  'publishing_verified',true,'publishing_verified_formats',(SELECT jsonb_agg(v) FROM(
    SELECT DISTINCT jsonb_array_elements(coalesce(verified_permissions->'publishing_verified_formats','[]')||jsonb_build_array(kind)) v) x))
 WHERE id=target_uuid;
 UPDATE lead.platform_capabilities SET technical_state='verified',adapter_version=release,evidence_ref=p_action::text,reviewed_at=now()
 WHERE platform=channel AND action_type='publish' AND target_type=(SELECT target_type FROM lead.account_targets WHERE id=target_uuid);
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION lead.growth_confirm_publication(uuid) FROM PUBLIC;

CREATE FUNCTION lead.growth_owner_fence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owns boolean;
BEGIN
 SELECT a.onboarding_state#>>'{growth_runtime,decision,engine}' IN ('growth','migrating') INTO owns
  FROM lead.agents a WHERE a.id=NEW.agent_id AND a.tenant_id=NEW.tenant_id;
 IF NOT coalesce(owns,false) THEN RETURN NEW; END IF;
 IF TG_TABLE_NAME='tasks' THEN
  IF TG_OP='INSERT' AND (NEW.type LIKE 'operating.%' OR NEW.type LIKE 'community.%'
    OR NEW.type IN ('channel.clock','dispatch.channel','content.prepare','media.generate','account.setup','connection.verify_publish'))
  THEN RAISE EXCEPTION 'legacy_business_owner_retired'; END IF;
  IF TG_OP='UPDATE' AND OLD.type LIKE 'growth.%' AND current_user<>'lead_owner'
   AND current_setting('app.growth_protocol',true) IS DISTINCT FROM '1'
  THEN RAISE EXCEPTION 'stale_growth_writer'; END IF;
  IF TG_OP='UPDATE' AND OLD.type NOT LIKE 'growth.%' AND (
    (NEW.result->>'reply_state'='dispatching' AND OLD.result->>'reply_state' IS DISTINCT FROM 'dispatching') OR
    (NEW.result->>'join_state'='dispatching' AND OLD.result->>'join_state' IS DISTINCT FROM 'dispatching'))
  THEN RAISE EXCEPTION 'legacy_native_owner_retired'; END IF;
  IF TG_OP='UPDATE' AND OLD.type='community.engage' AND (
    OLD.result->>'reply_state' IN ('dispatching','unknown','confirmed') OR
    OLD.result->>'join_state' IN ('dispatching','unknown','pending_approval','confirmed')) AND (
    NEW.input->'reply' IS DISTINCT FROM OLD.input->'reply' OR
    NEW.input->'candidate' IS DISTINCT FROM OLD.input->'candidate' OR
    NEW.input->>'account_id' IS DISTINCT FROM OLD.input->>'account_id' OR
    NEW.input->>'external_account_id' IS DISTINCT FROM OLD.input->>'external_account_id' OR
    NEW.result->>'target_id' IS DISTINCT FROM OLD.result->>'target_id')
  THEN RAISE EXCEPTION 'original_native_intent_immutable'; END IF;
 ELSE
  IF TG_OP='INSERT' AND NEW.intent->>'growth_runtime' IS DISTINCT FROM '1'
  THEN RAISE EXCEPTION 'legacy_business_owner_retired'; END IF;
  IF TG_OP='UPDATE' AND OLD.state IN ('prepared','not_applied') AND NEW.state='dispatching'
   AND NEW.intent->>'growth_runtime' IS DISTINCT FROM '1'
  THEN RAISE EXCEPTION 'legacy_business_owner_retired'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER growth_task_owner BEFORE INSERT OR UPDATE ON lead.tasks FOR EACH ROW EXECUTE FUNCTION lead.growth_owner_fence();
CREATE TRIGGER growth_action_owner BEFORE INSERT OR UPDATE ON lead.actions FOR EACH ROW EXECUTE FUNCTION lead.growth_owner_fence();

-- The schema fence precedes application replacement. Existing runners cannot
-- create a fresh send while the operator transfers each resource's ownership.
UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{growth_runtime}',
 '{"decision":{"engine":"migrating","generation":0}}'::jsonb)
 WHERE onboarding_state#>>'{growth_runtime,decision,engine}' IS NULL;
