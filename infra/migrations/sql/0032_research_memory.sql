CREATE TABLE lead.research_allowances (
 tenant_id uuid NOT NULL REFERENCES lead.tenants(id), connection_ref text NOT NULL,
 period_key text NOT NULL, unit text NOT NULL, limit_units bigint NOT NULL CHECK(limit_units>=0),
 reserved_units bigint NOT NULL DEFAULT 0 CHECK(reserved_units>=0),
 spent_units bigint NOT NULL DEFAULT 0 CHECK(spent_units>=0),
 PRIMARY KEY(tenant_id,connection_ref,period_key)
);
ALTER TABLE lead.research_allowances ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.research_allowances FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON lead.research_allowances USING(tenant_id=lead.ctx_tenant()) WITH CHECK(tenant_id=lead.ctx_tenant());
CREATE TABLE lead.experiences (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, agent_id uuid NOT NULL,
 extraction_key text NOT NULL, kind text NOT NULL CHECK(kind IN ('research','business','operation')),
 problem text NOT NULL, conclusion text NOT NULL, conditions jsonb NOT NULL DEFAULT '{}',
 evidence jsonb NOT NULL DEFAULT '[]', source_document_id uuid,
 counterevidence text, checked_at timestamptz NOT NULL DEFAULT now(), review_at timestamptz,
 version bigint NOT NULL DEFAULT 1, supersedes uuid,
 revoked_at timestamptz, public_projection jsonb,
 search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple',problem||' '||conclusion)) STORED,
 UNIQUE(tenant_id,agent_id,id), UNIQUE(tenant_id,agent_id,extraction_key),
 FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id),
 FOREIGN KEY(tenant_id,agent_id,source_document_id) REFERENCES lead.knowledge_documents(tenant_id,agent_id,id),
 FOREIGN KEY(tenant_id,agent_id,supersedes) REFERENCES lead.experiences(tenant_id,agent_id,id)
);
CREATE INDEX experience_search ON lead.experiences USING gin(search_vector);
ALTER TABLE lead.experiences ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead.experiences FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_scope ON lead.experiences USING(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent())) WITH CHECK(tenant_id=lead.ctx_tenant() AND (lead.ctx_agent() IS NULL OR agent_id=lead.ctx_agent()));
CREATE POLICY member_scope ON lead.experiences AS RESTRICTIVE USING(lead.member_agent_allowed(agent_id)) WITH CHECK(lead.member_agent_allowed(agent_id));
-- The only cross-project read returns an explicit public projection, never the
-- original conclusion, document, action, customer information or evidence body.
CREATE FUNCTION lead.public_experiences(p_agent uuid) RETURNS TABLE(id uuid, projection jsonb, checked_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT e.id,e.public_projection,e.checked_at FROM lead.experiences e
 WHERE lead.ctx_agent()=p_agent AND e.tenant_id=lead.ctx_tenant() AND e.agent_id<>p_agent
 AND lead.member_agent_allowed(p_agent) AND lead.member_agent_allowed(e.agent_id)
 AND e.revoked_at IS NULL AND e.public_projection IS NOT NULL AND (e.review_at IS NULL OR e.review_at>now())
 AND EXISTS(SELECT 1 FROM lead.agents a WHERE a.id=e.agent_id AND a.tenant_id=e.tenant_id AND a.desired_state<>'archived')
 AND (e.source_document_id IS NULL OR EXISTS(SELECT 1 FROM lead.knowledge_documents d WHERE d.id=e.source_document_id AND d.tenant_id=e.tenant_id AND d.agent_id=e.agent_id AND d.source_type='public_read' AND d.deleted_at IS NULL));
$$;
REVOKE ALL ON FUNCTION lead.public_experiences(uuid) FROM PUBLIC;

-- Sharing is a controlled projection even if another trusted writer bypasses
-- the Python helper. It cannot turn a private document into a public source.
CREATE FUNCTION lead.check_experience_projection() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lead AS $$
DECLARE d lead.knowledge_documents; body text; projection jsonb;
BEGIN
 projection=NEW.public_projection;
 IF projection IS NULL THEN RETURN NEW; END IF;
 SELECT * INTO d FROM lead.knowledge_documents WHERE id=NEW.source_document_id
   AND tenant_id=NEW.tenant_id AND agent_id=NEW.agent_id AND deleted_at IS NULL;
 SELECT string_agg(k.body,' ' ORDER BY ordinal) INTO body FROM lead.knowledge_chunks k
   WHERE k.document_id=d.id AND k.tenant_id=NEW.tenant_id AND k.agent_id=NEW.agent_id;
 IF jsonb_typeof(projection)<>'object' OR projection - ARRAY['problem','conclusion','conditions','sources'] <> '{}'::jsonb
   OR jsonb_typeof(projection->'conditions') IS DISTINCT FROM 'object'
   OR EXISTS(SELECT 1 FROM jsonb_each_text(projection->'conditions') item
     WHERE item.key NOT IN ('product','audience','region','language','value_behavior','location','operation')
       OR position(lower(item.value) IN lower(coalesce(body,'')))=0)
   OR d.id IS NULL OR d.source_type<>'public_read' OR projection->>'problem' IS DISTINCT FROM d.title
   OR projection->'sources' IS DISTINCT FROM jsonb_build_array(d.source_ref)
   OR length(coalesce(projection->>'conclusion','')) NOT BETWEEN 1 AND 500
   OR position(regexp_replace(projection->>'conclusion','\s+',' ','g') IN regexp_replace(coalesce(body,''),'\s+',' ','g'))=0
 THEN RAISE EXCEPTION 'public_experience_source_required'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER experience_projection BEFORE INSERT OR UPDATE OF public_projection,source_document_id
 ON lead.experiences FOR EACH ROW EXECUTE FUNCTION lead.check_experience_projection();
