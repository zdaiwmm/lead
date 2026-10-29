CREATE TABLE lead.auth_sessions(
 secret_sha256 text PRIMARY KEY CHECK(secret_sha256 ~ '^[a-f0-9]{64}$'),
 tenant_id uuid NOT NULL,issuer text NOT NULL,subject text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL,
 revoked_at timestamptz,
 FOREIGN KEY(tenant_id,issuer,subject) REFERENCES lead.memberships(tenant_id,oidc_issuer,oidc_subject)
);
CREATE TABLE lead.oauth_transactions(
 state_sha256 text PRIMARY KEY,browser_sha256 text NOT NULL,encrypted_payload bytea NOT NULL,
 expires_at timestamptz NOT NULL,used_at timestamptz
);
ALTER TABLE lead.auth_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.auth_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE lead.oauth_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.oauth_transactions FORCE ROW LEVEL SECURITY;
CREATE INDEX auth_session_expiry ON lead.auth_sessions(expires_at) WHERE revoked_at IS NULL;
CREATE INDEX oauth_expiry ON lead.oauth_transactions(expires_at);

CREATE FUNCTION lead.auth_enroll(p_issuer text,p_subject text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE workspace uuid;
BEGIN
 IF length(p_issuer)>2000 OR length(p_subject)>500 OR length(p_subject)=0 THEN RAISE EXCEPTION 'Invalid identity'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(p_issuer||chr(31)||p_subject,0));
 SELECT tenant_id INTO workspace FROM lead.memberships m JOIN lead.tenants t ON t.id=m.tenant_id WHERE oidc_issuer=p_issuer AND oidc_subject=p_subject AND status='active' AND t.deleted_at IS NULL ORDER BY m.created_at,tenant_id LIMIT 1;
 IF workspace IS NULL THEN
   IF EXISTS(SELECT 1 FROM lead.memberships WHERE oidc_issuer=p_issuer AND oidc_subject=p_subject) THEN RETURN NULL; END IF;
   INSERT INTO lead.tenants(name) VALUES('我的工作室') RETURNING id INTO workspace;
   INSERT INTO lead.memberships(tenant_id,oidc_issuer,oidc_subject,role) VALUES(workspace,p_issuer,p_subject,'owner');
 END IF;
 RETURN workspace;
END $$;
CREATE FUNCTION lead.auth_create_session(p_hash text,p_tenant uuid,p_issuer text,p_subject text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM lead.memberships m JOIN lead.tenants t ON t.id=m.tenant_id WHERE m.tenant_id=p_tenant AND oidc_issuer=p_issuer AND oidc_subject=p_subject AND m.status='active' AND t.deleted_at IS NULL) THEN RETURN false; END IF;
 INSERT INTO lead.auth_sessions(secret_sha256,tenant_id,issuer,subject,expires_at) VALUES(p_hash,p_tenant,p_issuer,p_subject,now()+interval '1 day');
 RETURN true;
END $$;
CREATE FUNCTION lead.auth_session(p_hash text) RETURNS TABLE(tenant_id uuid,role text,issuer text,subject text) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT s.tenant_id,m.role,s.issuer,s.subject FROM lead.auth_sessions s JOIN lead.memberships m ON m.tenant_id=s.tenant_id AND m.oidc_issuer=s.issuer AND m.oidc_subject=s.subject JOIN lead.tenants t ON t.id=s.tenant_id WHERE s.secret_sha256=p_hash AND s.revoked_at IS NULL AND s.expires_at>now() AND m.status='active' AND t.deleted_at IS NULL
$$;
CREATE FUNCTION lead.auth_logout(p_hash text) RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$ UPDATE lead.auth_sessions SET revoked_at=now() WHERE secret_sha256=p_hash $$;
CREATE FUNCTION lead.auth_begin(p_state text,p_browser text,p_payload bytea) RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 INSERT INTO lead.oauth_transactions(state_sha256,browser_sha256,encrypted_payload,expires_at) VALUES(p_state,p_browser,p_payload,now()+interval '5 minutes')
$$;
CREATE FUNCTION lead.auth_consume(p_state text,p_browser text) RETURNS bytea LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE payload bytea;
BEGIN
 UPDATE lead.oauth_transactions SET used_at=now() WHERE state_sha256=p_state AND browser_sha256=p_browser AND used_at IS NULL AND expires_at>now() RETURNING encrypted_payload INTO payload;
 RETURN payload;
END $$;
CREATE FUNCTION lead.auth_members(p_hash text) RETURNS TABLE(issuer text,subject text,role text,status text) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT m.oidc_issuer,m.oidc_subject,m.role,m.status FROM lead.memberships m JOIN lead.auth_session(p_hash) s ON s.tenant_id=m.tenant_id AND s.role='owner' ORDER BY m.created_at
$$;
CREATE FUNCTION lead.auth_member_change(p_hash text,p_issuer text,p_subject text,p_role text,p_status text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE actor record; existing record;
BEGIN
 SELECT * INTO actor FROM lead.auth_session(p_hash);
 IF actor IS NULL OR actor.role<>'owner' OR actor.issuer<>p_issuer OR p_role NOT IN ('owner','operator','viewer') OR p_status NOT IN ('active','revoked') OR length(p_subject) NOT BETWEEN 1 AND 500 THEN RETURN false; END IF;
 PERFORM 1 FROM lead.tenants WHERE id=actor.tenant_id FOR UPDATE;
 SELECT * INTO existing FROM lead.memberships WHERE tenant_id=actor.tenant_id AND oidc_issuer=p_issuer AND oidc_subject=p_subject;
 IF existing.role='owner' AND existing.status='active' AND (p_role<>'owner' OR p_status<>'active') AND (SELECT count(*) FROM lead.memberships WHERE tenant_id=actor.tenant_id AND role='owner' AND status='active')<=1 THEN RETURN false; END IF;
 INSERT INTO lead.memberships(tenant_id,oidc_issuer,oidc_subject,role,status) VALUES(actor.tenant_id,p_issuer,p_subject,p_role,p_status) ON CONFLICT(tenant_id,oidc_issuer,oidc_subject) DO UPDATE SET role=excluded.role,status=excluded.status;
 INSERT INTO lead.audit_events(tenant_id,actor_type,actor_ref,type,object_ref,payload,trace_id) VALUES(actor.tenant_id,'oidc',actor.subject,'membership.changed',p_subject,jsonb_build_object('role',p_role,'status',p_status),gen_random_uuid()::text);
 RETURN true;
END $$;
CREATE FUNCTION lead.auth_workspaces(p_hash text) RETURNS TABLE(id uuid,name text,role text) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT t.id,t.name,m.role FROM lead.auth_session(p_hash) s JOIN lead.memberships m ON m.oidc_issuer=s.issuer AND m.oidc_subject=s.subject AND m.status='active' JOIN lead.tenants t ON t.id=m.tenant_id AND t.deleted_at IS NULL ORDER BY m.created_at,t.id
$$;
CREATE FUNCTION lead.auth_switch(p_hash text,p_tenant uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM lead.auth_workspaces(p_hash) WHERE id=p_tenant) THEN RETURN false; END IF;
 UPDATE lead.auth_sessions SET tenant_id=p_tenant WHERE secret_sha256=p_hash;
 RETURN true;
END $$;
CREATE FUNCTION lead.execution_agent_tenant(p_agent uuid) RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$ SELECT tenant_id FROM lead.agents WHERE id=p_agent $$;
CREATE FUNCTION lead.execution_tenants() RETURNS TABLE(id uuid) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$ SELECT id FROM lead.tenants WHERE deleted_at IS NULL ORDER BY created_at $$;
CREATE FUNCTION lead.public_ref_tenant(p_kind text,p_reference text) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
DECLARE workspace uuid;
BEGIN
 IF p_kind='link' THEN SELECT l.tenant_id INTO workspace FROM lead.tracking_links l JOIN lead.agents a ON a.id=l.agent_id WHERE l.public_code=p_reference AND l.state='active' AND a.desired_state<>'archived';
 ELSIF p_kind='analytics' THEN SELECT n.tenant_id INTO workspace FROM lead.analytics_connections n JOIN lead.agents a ON a.id=n.agent_id WHERE n.id::text=p_reference AND n.state<>'disconnected' AND a.desired_state<>'archived';
 ELSIF p_kind='conversion' THEN SELECT k.tenant_id INTO workspace FROM lead.analytics_credentials k JOIN lead.agents a ON a.id=k.agent_id WHERE k.secret_sha256=p_reference AND k.state='active' AND a.desired_state<>'archived'; END IF;
 RETURN workspace;
END $$;
REVOKE ALL ON lead.auth_sessions,lead.oauth_transactions FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION lead.auth_enroll(text,text),lead.auth_create_session(text,uuid,text,text),lead.auth_session(text),lead.auth_logout(text),lead.auth_begin(text,text,bytea),lead.auth_consume(text,text),lead.auth_members(text),lead.auth_member_change(text,text,text,text,text),lead.auth_workspaces(text),lead.auth_switch(text,uuid),lead.execution_agent_tenant(uuid),lead.execution_tenants(),lead.public_ref_tenant(text,text) FROM PUBLIC;
