-- No existing request, device, task, thread or default is reassigned here.
ALTER TABLE lead.admin_configs DROP CONSTRAINT admin_configs_kind_check;
ALTER TABLE lead.admin_configs ADD CHECK(kind IN
 ('budget','strategy','onboarding','models','brand','runtime','notifications','channel_rule',
  'attribution','experience','model_catalog','member_access','pricing','external_notifications','execution'));
ALTER TABLE lead.local_executor_projects ADD COLUMN execution_override text NOT NULL DEFAULT 'inherit'
 CHECK(execution_override IN ('inherit','local_device','legacy'));

CREATE FUNCTION lead.ctx_executor_node() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT NULLIF(current_setting('app.executor_node_id',true),'')::uuid
$$;
CREATE FUNCTION lead.ctx_executor_grant() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT NULLIF(current_setting('app.executor_grant_id',true),'')::uuid
$$;
CREATE FUNCTION lead.ctx_executor_studio() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT NULLIF(current_setting('app.executor_studio_id',true),'')::uuid
$$;
-- Project transactions may read workspace settings but their UPDATE RLS cannot
-- row-lock them. Lock this single tenant's route without widening that policy.
CREATE FUNCTION lead.executor_policy() RETURNS TABLE(body jsonb,published_version bigint)
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT c.body,c.published_version::bigint FROM lead.admin_configs c
 WHERE c.tenant_id=lead.ctx_tenant() AND c.config_key='execution/default' FOR SHARE
$$;

CREATE TABLE lead.executor_nodes (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), control_tenant_id uuid NOT NULL REFERENCES lead.tenants(id),
 allowed_tenants uuid[] NOT NULL CHECK(cardinality(allowed_tenants) BETWEEN 1 AND 1000),
 deployment_ref text NOT NULL, name text NOT NULL, platform text NOT NULL DEFAULT '',
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','revoked')),
 enrollment_hash text, enrollment_expires_at timestamptz,
 credential_hash text, credential_generation bigint NOT NULL DEFAULT 1 CHECK(credential_generation>0),
 epoch bigint NOT NULL DEFAULT 1 CHECK(epoch>0), instance_id text,
 capabilities jsonb NOT NULL DEFAULT '{}', last_seen_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz,
 CHECK(control_tenant_id=ANY(allowed_tenants))
);
ALTER TABLE lead.executor_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.executor_nodes FORCE ROW LEVEL SECURITY;
CREATE POLICY node_read ON lead.executor_nodes FOR SELECT USING
 (id=lead.ctx_executor_node() OR lead.ctx_tenant()=ANY(allowed_tenants));
CREATE POLICY node_insert ON lead.executor_nodes FOR INSERT WITH CHECK
 (lead.ctx_executor_node() IS NULL AND control_tenant_id=lead.ctx_tenant() AND lead.ctx_agent() IS NULL);
CREATE POLICY node_control ON lead.executor_nodes FOR UPDATE USING
 (lead.ctx_executor_node() IS NULL AND control_tenant_id=lead.ctx_tenant() AND lead.ctx_agent() IS NULL)
 WITH CHECK(lead.ctx_executor_node() IS NULL AND control_tenant_id=lead.ctx_tenant());
CREATE POLICY node_heartbeat ON lead.executor_nodes FOR UPDATE USING(id=lead.ctx_executor_node())
 WITH CHECK(id=lead.ctx_executor_node());

CREATE TABLE lead.executor_node_grants (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), node_id uuid NOT NULL REFERENCES lead.executor_nodes(id),
 tenant_id uuid NOT NULL REFERENCES lead.tenants(id), scope_kind text NOT NULL CHECK(scope_kind IN ('project','studio')),
 agent_id uuid, device_id uuid, studio_id uuid, generation bigint NOT NULL DEFAULT 1 CHECK(generation>0),
 epoch bigint NOT NULL DEFAULT 1 CHECK(epoch>0), instance_id text,
 policy_revision text NOT NULL DEFAULT 'execution/default@0',
 state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')),
 created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz,
 CHECK((scope_kind='project' AND agent_id IS NOT NULL AND device_id IS NOT NULL AND studio_id IS NULL)
    OR (scope_kind='studio' AND agent_id IS NULL AND device_id IS NULL AND studio_id=tenant_id)),
 UNIQUE(tenant_id,studio_id,id), UNIQUE(tenant_id,agent_id,id),
 FOREIGN KEY(tenant_id,agent_id,device_id) REFERENCES lead.local_executor_devices(tenant_id,agent_id,id)
);
CREATE UNIQUE INDEX executor_node_project_grant ON lead.executor_node_grants(node_id,tenant_id,agent_id)
 WHERE scope_kind='project';
CREATE UNIQUE INDEX executor_node_studio_grant ON lead.executor_node_grants(node_id,tenant_id,studio_id)
 WHERE scope_kind='studio';
CREATE UNIQUE INDEX executor_managed_device ON lead.executor_node_grants(tenant_id,agent_id,device_id)
 WHERE scope_kind='project';
ALTER TABLE lead.executor_node_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.executor_node_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY grant_read ON lead.executor_node_grants FOR SELECT USING
 (node_id=lead.ctx_executor_node() OR tenant_id=lead.ctx_tenant()
  AND (agent_id IS NULL OR lead.member_agent_allowed(agent_id)));
CREATE POLICY grant_insert ON lead.executor_node_grants FOR INSERT WITH CHECK
 (lead.ctx_executor_node() IS NULL AND tenant_id=lead.ctx_tenant()
  AND (agent_id IS NULL OR lead.member_agent_allowed(agent_id))
  AND EXISTS(SELECT 1 FROM lead.executor_nodes n WHERE n.id=node_id AND tenant_id=ANY(n.allowed_tenants)));
CREATE POLICY grant_control ON lead.executor_node_grants FOR UPDATE USING
 (lead.ctx_executor_node() IS NULL AND tenant_id=lead.ctx_tenant()
  AND (agent_id IS NULL OR lead.member_agent_allowed(agent_id)))
 WITH CHECK(lead.ctx_executor_node() IS NULL AND tenant_id=lead.ctx_tenant());

-- The Node cannot grant itself a workspace or modify control fields. Controlled
-- and authenticated code uses these routines; runtime direct writes are revoked
-- again by provision_security after its generic GRANT, not just by this migration.
CREATE FUNCTION lead.executor_node_write(p_id uuid,p_control uuid,p_tenants uuid[],p_deployment text,p_name text,
 p_enrollment_hash text,p_expires timestamptz) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lead AS $$
BEGIN
 IF lead.ctx_executor_node() IS NOT NULL OR lead.ctx_tenant() IS DISTINCT FROM p_control
    OR lead.ctx_agent() IS NOT NULL OR NOT p_control=ANY(p_tenants)
    OR EXISTS(SELECT 1 FROM unnest(p_tenants) t WHERE NOT EXISTS(SELECT 1 FROM lead.tenants w WHERE w.id=t AND w.deleted_at IS NULL))
 THEN RAISE EXCEPTION 'executor_scope_forbidden' USING ERRCODE='42501'; END IF;
 INSERT INTO lead.executor_nodes(id,control_tenant_id,allowed_tenants,deployment_ref,name,enrollment_hash,enrollment_expires_at)
 VALUES(p_id,p_control,p_tenants,p_deployment,p_name,p_enrollment_hash,p_expires);
END $$;

CREATE TABLE lead.executor_studio_requests (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES lead.tenants(id),
 studio_id uuid NOT NULL CHECK(studio_id=tenant_id), grant_id uuid NOT NULL,
 request_key text NOT NULL, request_hash text NOT NULL, session_key text NOT NULL,
 actor_type text NOT NULL CHECK(actor_type='user'), actor_ref text NOT NULL, actor jsonb NOT NULL,
 purpose text NOT NULL CHECK(purpose='website_brief'), payload jsonb NOT NULL, scope jsonb NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN
 ('queued','running','waiting_input','completed','failed','interrupted','execution_unknown','canceled')),
 result jsonb, usage jsonb, thread_id text, turn_id text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
 UNIQUE(tenant_id,studio_id,id), UNIQUE(tenant_id,studio_id,request_key),
 FOREIGN KEY(tenant_id,studio_id,grant_id) REFERENCES lead.executor_node_grants(tenant_id,studio_id,id)
);
CREATE INDEX executor_studio_session ON lead.executor_studio_requests(tenant_id,studio_id,session_key,created_at);
CREATE TABLE lead.executor_studio_commands (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, studio_id uuid NOT NULL,
 request_id uuid NOT NULL, command_key text NOT NULL, command_hash text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('run','steer','interrupt','respond')), payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), acknowledged_at timestamptz,
 UNIQUE(tenant_id,studio_id,request_id,command_key),
 FOREIGN KEY(tenant_id,studio_id,request_id) REFERENCES lead.executor_studio_requests(tenant_id,studio_id,id)
);
CREATE TABLE lead.executor_studio_events (
 tenant_id uuid NOT NULL, studio_id uuid NOT NULL, request_id uuid NOT NULL,
 event_id text NOT NULL, event_hash text NOT NULL, method text NOT NULL, params jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,studio_id,event_id),
 FOREIGN KEY(tenant_id,studio_id,request_id) REFERENCES lead.executor_studio_requests(tenant_id,studio_id,id)
);

CREATE FUNCTION lead.executor_studio_row(p_tenant uuid,p_studio uuid,p_actor text,p_grant uuid) RETURNS boolean
 LANGUAGE sql STABLE AS $$
 SELECT p_tenant=lead.ctx_tenant() AND p_studio=lead.ctx_executor_studio() AND (
  (lead.ctx_executor_node() IS NULL AND p_actor=NULLIF(current_setting('app.executor_studio_actor',true),''))
  OR (p_grant=lead.ctx_executor_grant() AND EXISTS(SELECT 1 FROM lead.executor_node_grants g
      WHERE g.id=p_grant AND g.node_id=lead.ctx_executor_node() AND g.scope_kind='studio'
       AND g.tenant_id=p_tenant AND g.studio_id=p_studio AND g.state='active')))
$$;
ALTER TABLE lead.executor_studio_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.executor_studio_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY studio_scope ON lead.executor_studio_requests USING
 (lead.executor_studio_row(tenant_id,studio_id,actor_ref,grant_id)) WITH CHECK
 (lead.ctx_executor_node() IS NULL AND lead.executor_studio_row(tenant_id,studio_id,actor_ref,grant_id));
-- Service execution may update original rows, never create new studio work.
CREATE POLICY studio_service_update ON lead.executor_studio_requests FOR UPDATE USING
 (lead.ctx_executor_node() IS NOT NULL AND lead.executor_studio_row(tenant_id,studio_id,actor_ref,grant_id))
 WITH CHECK(lead.ctx_executor_node() IS NOT NULL AND lead.executor_studio_row(tenant_id,studio_id,actor_ref,grant_id));
DO $$ DECLARE tab text; BEGIN
 FOREACH tab IN ARRAY ARRAY['executor_studio_commands','executor_studio_events'] LOOP
  EXECUTE format('ALTER TABLE lead.%I ENABLE ROW LEVEL SECURITY',tab);
  EXECUTE format('ALTER TABLE lead.%I FORCE ROW LEVEL SECURITY',tab);
  EXECUTE format('CREATE POLICY studio_child_scope ON lead.%1$I USING(EXISTS(SELECT 1 FROM lead.executor_studio_requests r
   WHERE (r.tenant_id,r.studio_id,r.id)=(%1$I.tenant_id,%1$I.studio_id,%1$I.request_id))) WITH CHECK(EXISTS(SELECT 1 FROM lead.executor_studio_requests r
   WHERE (r.tenant_id,r.studio_id,r.id)=(%1$I.tenant_id,%1$I.studio_id,%1$I.request_id)))',tab);
 END LOOP;
END $$;

CREATE FUNCTION lead.executor_node_activate(p_id uuid,p_enrollment text,p_credential text,p_platform text,p_instance text,p_caps jsonb)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
BEGIN
 IF lead.ctx_executor_node() IS DISTINCT FROM p_id THEN RAISE EXCEPTION 'executor_scope_forbidden' USING ERRCODE='42501'; END IF;
 UPDATE lead.executor_nodes SET state='active',credential_hash=p_credential,platform=p_platform,
  instance_id=p_instance,capabilities=p_caps,last_seen_at=now(),enrollment_hash=NULL,enrollment_expires_at=NULL
 WHERE id=p_id AND state='pending' AND enrollment_hash=p_enrollment AND enrollment_expires_at>now();
 IF NOT FOUND THEN RAISE EXCEPTION 'executor_node_unavailable' USING ERRCODE='42501'; END IF;
END $$;
CREATE FUNCTION lead.executor_node_control(p_id uuid,p_action text,p_credential text) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
BEGIN
 IF lead.ctx_executor_node() IS NOT NULL OR lead.ctx_agent() IS NOT NULL
  OR NOT EXISTS(SELECT 1 FROM lead.executor_nodes WHERE id=p_id AND control_tenant_id=lead.ctx_tenant())
  OR p_action NOT IN ('revoke','rotate') THEN RAISE EXCEPTION 'executor_scope_forbidden' USING ERRCODE='42501'; END IF;
 UPDATE lead.executor_nodes SET state=CASE WHEN p_action='revoke' THEN 'revoked' ELSE 'active' END,
  credential_hash=CASE WHEN p_action='revoke' THEN NULL ELSE p_credential END,
  credential_generation=credential_generation+1,epoch=epoch+1,
  instance_id=NULL,enrollment_hash=NULL,enrollment_expires_at=NULL,
  revoked_at=CASE WHEN p_action='revoke' THEN now() ELSE NULL END WHERE id=p_id;
END $$;
CREATE FUNCTION lead.executor_node_instance(p_id uuid,p_epoch bigint,p_instance text) RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
BEGIN
 IF lead.ctx_executor_node() IS DISTINCT FROM p_id THEN RETURN false; END IF;
 UPDATE lead.executor_nodes SET instance_id=p_instance WHERE id=p_id AND state='active' AND epoch=p_epoch
  AND (instance_id IS NULL OR instance_id=p_instance);
 RETURN FOUND;
END $$;

CREATE FUNCTION lead.executor_grant_ensure(p_node uuid,p_tenant uuid,p_agent uuid) RETURNS uuid
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE n lead.executor_nodes; g lead.executor_node_grants; device uuid; policy_version bigint;
BEGIN
 IF p_tenant IS DISTINCT FROM lead.ctx_tenant()
  OR (lead.ctx_executor_node() IS NOT NULL AND lead.ctx_executor_node() IS DISTINCT FROM p_node)
  OR (p_agent IS NOT NULL AND lead.ctx_executor_node() IS NULL AND NOT lead.member_agent_allowed(p_agent))
 THEN RAISE EXCEPTION 'executor_scope_forbidden' USING ERRCODE='42501'; END IF;
 SELECT * INTO n FROM lead.executor_nodes WHERE id=p_node FOR SHARE;
 IF NOT FOUND OR n.state<>'active' OR NOT p_tenant=ANY(n.allowed_tenants)
  OR NOT EXISTS(SELECT 1 FROM lead.admin_configs WHERE tenant_id=p_tenant AND config_key='execution/default'
   AND body->>'source'='managed_node' AND body->>'node_id'=p_node::text)
  OR NOT EXISTS(SELECT 1 FROM lead.tenants WHERE id=p_tenant AND deleted_at IS NULL)
 THEN RAISE EXCEPTION 'executor_scope_forbidden' USING ERRCODE='42501'; END IF;
 IF p_agent IS NOT NULL AND NOT EXISTS(SELECT 1 FROM lead.agents a LEFT JOIN lead.local_executor_projects p
    ON p.tenant_id=a.tenant_id AND p.agent_id=a.id WHERE a.id=p_agent AND a.tenant_id=p_tenant
    AND a.desired_state<>'archived' AND coalesce(p.execution_override,'inherit')='inherit')
 THEN RAISE EXCEPTION 'executor_scope_forbidden' USING ERRCODE='42501'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(p_node::text||p_tenant::text||coalesce(p_agent::text,'studio'),0));
 SELECT * INTO g FROM lead.executor_node_grants WHERE node_id=p_node AND tenant_id=p_tenant
  AND ((p_agent IS NULL AND scope_kind='studio') OR (p_agent IS NOT NULL AND agent_id=p_agent));
 IF FOUND THEN RETURN g.id; END IF;
 IF p_agent IS NOT NULL THEN
  INSERT INTO lead.local_executor_devices(tenant_id,agent_id,name,platform,state,capabilities,last_seen_at)
   VALUES(p_tenant,p_agent,n.name,'managed-node','active',n.capabilities,n.last_seen_at) RETURNING id INTO device;
 END IF;
 SELECT published_version INTO policy_version FROM lead.admin_configs WHERE tenant_id=p_tenant AND config_key='execution/default';
 INSERT INTO lead.executor_node_grants(node_id,tenant_id,scope_kind,agent_id,device_id,studio_id,policy_revision)
  VALUES(p_node,p_tenant,CASE WHEN p_agent IS NULL THEN 'studio' ELSE 'project' END,p_agent,device,
   CASE WHEN p_agent IS NULL THEN p_tenant ELSE NULL END,'execution/default@'||policy_version) RETURNING id INTO device;
 RETURN device;
END $$;
CREATE FUNCTION lead.executor_catalog_projects(p_node uuid,p_tenant uuid) RETURNS TABLE(agent_id uuid)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT a.id FROM lead.agents a LEFT JOIN lead.local_executor_projects p
  ON p.tenant_id=a.tenant_id AND p.agent_id=a.id
 WHERE lead.ctx_executor_node()=p_node AND lead.ctx_tenant()=p_tenant
  AND a.tenant_id=p_tenant AND a.desired_state<>'archived' AND coalesce(p.execution_override,'inherit')='inherit'
  AND EXISTS(SELECT 1 FROM lead.executor_nodes n WHERE n.id=p_node AND n.state='active' AND p_tenant=ANY(n.allowed_tenants))
  AND EXISTS(SELECT 1 FROM lead.admin_configs c WHERE c.tenant_id=p_tenant AND c.config_key='execution/default'
   AND c.body->>'source'='managed_node' AND c.body->>'node_id'=p_node::text)
$$;
CREATE FUNCTION lead.executor_grant_instance(p_id uuid,p_generation bigint,p_epoch bigint,p_instance text) RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE g lead.executor_node_grants;
BEGIN
 SELECT * INTO g FROM lead.executor_node_grants WHERE id=p_id AND node_id=lead.ctx_executor_node() FOR UPDATE;
 IF NOT FOUND OR g.state<>'active' OR g.generation<>p_generation OR g.epoch<>p_epoch
  OR (g.instance_id IS NOT NULL AND g.instance_id<>p_instance) THEN RETURN false; END IF;
 UPDATE lead.executor_node_grants SET instance_id=p_instance WHERE id=p_id;
 IF g.device_id IS NOT NULL THEN
  UPDATE lead.local_executor_devices SET instance_id=p_instance WHERE id=g.device_id AND tenant_id=g.tenant_id
   AND agent_id=g.agent_id AND epoch=p_epoch AND state='active'
   AND (instance_id IS NULL OR instance_id=p_instance);
  IF NOT FOUND THEN RAISE EXCEPTION 'executor_epoch_changed' USING ERRCODE='42501'; END IF;
 END IF;
 RETURN true;
END $$;
CREATE FUNCTION lead.executor_grant_locked(p_id uuid) RETURNS SETOF lead.executor_node_grants
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
BEGIN
 RETURN QUERY SELECT * FROM lead.executor_node_grants WHERE id=p_id AND node_id=lead.ctx_executor_node()
  AND tenant_id=lead.ctx_tenant() FOR UPDATE;
END $$;
CREATE FUNCTION lead.executor_grant_revoke(p_id uuid) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE g lead.executor_node_grants;
BEGIN
 IF lead.ctx_executor_node() IS NOT NULL THEN RAISE EXCEPTION 'executor_scope_forbidden' USING ERRCODE='42501'; END IF;
 SELECT * INTO g FROM lead.executor_node_grants WHERE id=p_id AND tenant_id=lead.ctx_tenant() FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'executor_scope_forbidden' USING ERRCODE='42501'; END IF;
 UPDATE lead.executor_node_grants SET state='revoked',generation=generation+1,epoch=epoch+1,revoked_at=now() WHERE id=p_id;
 IF g.device_id IS NOT NULL THEN
  UPDATE lead.local_executor_devices SET epoch=epoch+1 WHERE id=g.device_id AND tenant_id=g.tenant_id AND agent_id=g.agent_id;
 END IF;
END $$;

CREATE FUNCTION lead.executor_request_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF lead.ctx_executor_node() IS NOT NULL THEN RAISE EXCEPTION 'executor_scope_forbidden'; END IF;
  IF TG_TABLE_NAME='local_executor_requests' THEN
   IF NEW.scope#>>'{execution_route,source}'='managed_node'
    AND NOT EXISTS(SELECT 1 FROM lead.executor_node_grants g WHERE g.id=(NEW.scope#>>'{execution_route,grant_id}')::uuid
    AND g.scope_kind='project' AND g.tenant_id=NEW.tenant_id AND g.agent_id=NEW.agent_id AND g.device_id=NEW.device_id
    AND g.state='active' AND g.generation=(NEW.scope#>>'{execution_route,grant_generation}')::bigint)
    THEN RAISE EXCEPTION 'executor_scope_forbidden'; END IF;
  END IF;
 ELSE
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.request_key IS DISTINCT FROM OLD.request_key
   OR NEW.request_hash IS DISTINCT FROM OLD.request_hash OR NEW.session_key IS DISTINCT FROM OLD.session_key
   OR NEW.payload IS DISTINCT FROM OLD.payload OR NEW.scope->'execution_route' IS DISTINCT FROM OLD.scope->'execution_route'
   THEN RAISE EXCEPTION 'executor_request_conflict'; END IF;
  IF TG_TABLE_NAME='local_executor_requests' THEN
   IF NEW.agent_id IS DISTINCT FROM OLD.agent_id OR NEW.task_id IS DISTINCT FROM OLD.task_id
    OR OLD.device_id IS NOT NULL AND NEW.device_id IS DISTINCT FROM OLD.device_id
    THEN RAISE EXCEPTION 'executor_request_conflict'; END IF;
  ELSE
   IF NEW.studio_id IS DISTINCT FROM OLD.studio_id OR NEW.grant_id IS DISTINCT FROM OLD.grant_id
    OR NEW.actor IS DISTINCT FROM OLD.actor OR NEW.actor_ref IS DISTINCT FROM OLD.actor_ref
    OR NEW.actor_type IS DISTINCT FROM OLD.actor_type OR NEW.purpose IS DISTINCT FROM OLD.purpose
    THEN RAISE EXCEPTION 'executor_request_conflict'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER executor_request_immutable BEFORE INSERT OR UPDATE ON lead.local_executor_requests
 FOR EACH ROW EXECUTE FUNCTION lead.executor_request_immutable();
CREATE TRIGGER executor_studio_immutable BEFORE INSERT OR UPDATE ON lead.executor_studio_requests
 FOR EACH ROW EXECUTE FUNCTION lead.executor_request_immutable();

CREATE FUNCTION lead.executor_project_row(p_tenant uuid,p_agent uuid,p_device uuid,p_scope jsonb) RETURNS boolean
 LANGUAGE sql STABLE AS $$
 SELECT lead.ctx_executor_node() IS NULL OR EXISTS(SELECT 1 FROM lead.executor_node_grants g
  WHERE g.id=lead.ctx_executor_grant() AND g.node_id=lead.ctx_executor_node() AND g.state='active'
   AND g.tenant_id=p_tenant AND g.agent_id=p_agent AND g.device_id=p_device
   AND p_scope#>>'{execution_route,source}'='managed_node'
   AND p_scope#>>'{execution_route,grant_id}'=g.id::text
   AND p_scope#>>'{execution_route,grant_generation}'=g.generation::text)
$$;
CREATE POLICY executor_node_request_scope ON lead.local_executor_requests AS RESTRICTIVE USING
 (lead.executor_project_row(tenant_id,agent_id,device_id,scope)) WITH CHECK
 (lead.executor_project_row(tenant_id,agent_id,device_id,scope));
CREATE POLICY executor_node_device_scope ON lead.local_executor_devices AS RESTRICTIVE USING
 (lead.ctx_executor_node() IS NULL OR EXISTS(SELECT 1 FROM lead.executor_node_grants g
  WHERE g.id=lead.ctx_executor_grant() AND g.node_id=lead.ctx_executor_node() AND g.state='active'
   AND g.tenant_id=local_executor_devices.tenant_id AND g.agent_id=local_executor_devices.agent_id AND g.device_id=local_executor_devices.id)) WITH CHECK
 (lead.ctx_executor_node() IS NULL OR EXISTS(SELECT 1 FROM lead.executor_node_grants g
  WHERE g.id=lead.ctx_executor_grant() AND g.node_id=lead.ctx_executor_node() AND g.state='active'
   AND g.tenant_id=local_executor_devices.tenant_id AND g.agent_id=local_executor_devices.agent_id AND g.device_id=local_executor_devices.id));
DO $$ DECLARE tab text; BEGIN
 FOREACH tab IN ARRAY ARRAY['local_executor_commands','local_executor_events'] LOOP
  EXECUTE format('CREATE POLICY executor_node_child_scope ON lead.%1$I AS RESTRICTIVE USING
   (lead.ctx_executor_node() IS NULL OR EXISTS(SELECT 1 FROM lead.local_executor_requests r
     WHERE (r.tenant_id,r.agent_id,r.id)=(%1$I.tenant_id,%1$I.agent_id,%1$I.request_id))) WITH CHECK
   (lead.ctx_executor_node() IS NULL OR EXISTS(SELECT 1 FROM lead.local_executor_requests r
     WHERE (r.tenant_id,r.agent_id,r.id)=(%1$I.tenant_id,%1$I.agent_id,%1$I.request_id)))',tab);
 END LOOP;
END $$;
