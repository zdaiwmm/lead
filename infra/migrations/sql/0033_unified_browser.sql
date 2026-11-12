-- Keep original account/action ownership and RLS; no second permission ledger.
ALTER TABLE lead.takeover_sessions ADD COLUMN control_mode text NOT NULL DEFAULT 'owner'
 CHECK(control_mode IN ('automatic','owner'));
ALTER TABLE lead.takeover_sessions ADD COLUMN challenge_version bigint NOT NULL DEFAULT 1 CHECK(challenge_version>0);
ALTER TABLE lead.takeover_sessions ADD COLUMN automatic_attempts integer NOT NULL DEFAULT 0 CHECK(automatic_attempts BETWEEN 0 AND 3);
ALTER TABLE lead.takeover_sessions ADD COLUMN connection_data jsonb NOT NULL DEFAULT '{}';
ALTER TABLE lead.takeover_sessions ADD COLUMN encrypted_login bytea;
ALTER TABLE lead.social_accounts ADD COLUMN connection_origin text;
DROP INDEX lead.one_account_controller;
CREATE UNIQUE INDEX one_account_controller ON lead.social_accounts
 (platform,(CASE WHEN platform='web' THEN connection_origin ELSE '' END),external_account_id)
 WHERE state IN ('connected','needs_auth','restricted') AND external_account_id IS NOT NULL;
ALTER TABLE lead.social_accounts ADD CONSTRAINT web_connection_origin_required
 CHECK(platform<>'web' OR connection_origin IS NOT NULL);
CREATE FUNCTION lead.clear_connection_login() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.state<>'active' OR NEW.expires_at<=now() OR NEW.completed_account_id IS NOT NULL THEN
  NEW.encrypted_login:=NULL;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER connection_login_lifetime BEFORE INSERT OR UPDATE ON lead.takeover_sessions
 FOR EACH ROW EXECUTE FUNCTION lead.clear_connection_login();
CREATE UNIQUE INDEX browser_request_original_action ON lead.action_attempts
 (tenant_id,agent_id,(observation#>>'{browser_request,profile_id}'),(observation#>>'{browser_request,fingerprint}'))
 WHERE observation#>>'{browser_request,fingerprint}' IS NOT NULL;

CREATE OR REPLACE FUNCTION lead.growth_complete_connection(p_agent uuid,p_takeover uuid,p_task uuid,p_input_seq bigint,
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
  OR NOT EXISTS(SELECT FROM lead.agents a WHERE a.id=p_agent AND a.control_version=takeover.control_version)
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
  RAISE EXCEPTION 'connection_identity_ambiguous';
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

CREATE OR REPLACE FUNCTION lead.growth_receipt_valid(p_action uuid) RETURNS boolean
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
   AND (NOT a.intent ? 'browser_method' OR EXISTS(SELECT FROM lead.action_attempts br WHERE br.action_id=a.id
      AND br.outcome='confirmed' AND br.observation#>>'{browser_receipt,author}'=s.external_account_id
      AND br.observation#>>'{browser_receipt,body}'=a.intent->>'body'
      AND br.observation#>>'{browser_receipt,url}'=a.external_url
      AND (a.type<>'reply' OR br.observation#>>'{browser_receipt,parent_url}'=a.intent->>'parent_url')))
   AND ((s.platform='lab' AND EXISTS(SELECT FROM lead.lab_posts l WHERE l.agent_id=a.agent_id AND l.operation_key=a.operation_key))
    OR (a.evidence_ref IS NOT NULL AND EXISTS(SELECT FROM lead.action_attempts ap WHERE ap.action_id=a.id
      AND ap.outcome='confirmed' AND ap.observed_account_id=s.external_account_id
      AND ap.observed_target_id=t.external_target_id AND ap.control_version=a.expected_control_version
      AND ap.started_at>=g.granted_at AND (g.revoked_at IS NULL OR ap.started_at<g.revoked_at)))));
$$;
REVOKE ALL ON FUNCTION lead.growth_receipt_valid(uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION lead.growth_confirm_publication(p_action uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE target_uuid uuid; channel text; kind text; release text; native_action lead.actions;
BEGIN
 IF NOT lead.growth_receipt_valid(p_action) THEN RETURN false; END IF;
 SELECT * INTO native_action FROM lead.actions WHERE id=p_action AND tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent();
 IF native_action.intent ? 'browser_method' THEN
  IF NOT EXISTS(SELECT FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id
    JOIN lead.authorization_grants g ON g.id=native_action.grant_id AND g.target_id=t.id
    WHERE t.id=native_action.target_id AND s.state='connected' AND t.state='active'
     AND s.binding_version=(native_action.intent->>'binding_version')::bigint
     AND t.verified_permissions->'browser_method'=native_action.intent->'browser_method'
     AND g.revoked_at IS NULL AND g.allowed_actions ? native_action.type)
  THEN RETURN false; END IF;
  UPDATE lead.account_targets SET verified_permissions=verified_permissions ||
   jsonb_build_object('browser_verified_action',p_action::text,'browser_verified_operation',native_action.type)
   WHERE id=native_action.target_id;
  RETURN true;
 END IF;
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

