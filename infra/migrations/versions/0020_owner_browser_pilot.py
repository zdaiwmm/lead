"""Explicit owner-bound browser pilot; no global platform permission expansion."""

from alembic import op

revision = "0020_owner_browser_pilot"
down_revision = "0019_operating_progress"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        """CREATE OR REPLACE FUNCTION lead.owner_browser_pilot_allowed(p_target uuid,p_grant uuid DEFAULT NULL) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
 SELECT EXISTS(
  SELECT 1 FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id AND s.agent_id=t.agent_id AND s.tenant_id=t.tenant_id
  JOIN lead.agents a ON a.id=t.agent_id AND a.tenant_id=t.tenant_id
  JOIN lead.authorization_grants g ON g.target_id=t.id AND g.agent_id=t.agent_id AND g.tenant_id=t.tenant_id AND g.revoked_at IS NULL
  CROSS JOIN LATERAL (SELECT a.onboarding_state->'browser_pilots'->t.id::text AS body) pilot
  WHERE t.id=p_target AND t.tenant_id=lead.ctx_tenant() AND t.agent_id=lead.ctx_agent() AND lead.member_agent_allowed(t.agent_id)
   AND a.desired_state='running' AND s.platform='tiktok' AND s.state='connected' AND s.automation_enabled AND t.state='active'
   AND (p_grant IS NULL OR g.id=p_grant) AND g.allowed_actions ? 'publish' AND g.allowed_actions ? 'upload_media'
   AND pilot.body->>'authority'='workspace_owner' AND pilot.body->>'method'='browser' AND pilot.body->>'state'='active'
   AND pilot.body->>'account_id'=s.id::text AND pilot.body->>'external_account_id'=s.external_account_id
   AND pilot.body->>'target_id'=t.id::text AND pilot.body->>'grant_id'=g.id::text
   AND (pilot.body->>'expires_at')::timestamptz>now()
 );
$$;
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
 JOIN lead.platform_capabilities pc ON pc.platform=s.platform AND pc.action_type='publish' AND pc.target_type=at.target_type AND (pc.permission_state='approved' OR (pc.permission_state='pending' AND lead.owner_browser_pilot_allowed(at.id,a.grant_id)))
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
"""
    )


def downgrade():
    raise RuntimeError("Preserve authorization and publication evidence")
