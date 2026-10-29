"""Managed exits, usage detail and operator recovery checkpoints."""

from alembic import op

revision = "0008_local_product"
down_revision = "0007_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""
      CREATE TABLE lead.proxy_pool(
        id uuid PRIMARY KEY, label text NOT NULL UNIQUE,cipher bytea NOT NULL,
        expected_ip inet NOT NULL UNIQUE,region text,timezone text,locale text,
        state text NOT NULL DEFAULT 'available' CHECK(state IN ('available','disabled','quarantined','retired')),
        expires_at timestamptz,created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE lead.proxy_assignments(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),pool_id uuid NOT NULL REFERENCES lead.proxy_pool(id),
        tenant_id uuid NOT NULL,agent_id uuid NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),released_at timestamptz,
        FOREIGN KEY(tenant_id,agent_id) REFERENCES lead.agents(tenant_id,id)
      );
      CREATE UNIQUE INDEX proxy_one_lease ON lead.proxy_assignments(pool_id) WHERE released_at IS NULL;
      CREATE UNIQUE INDEX agent_one_proxy ON lead.proxy_assignments(agent_id) WHERE released_at IS NULL;
      CREATE TABLE lead.recovery_cases(
        id uuid PRIMARY KEY,backup_id uuid NOT NULL,state text NOT NULL CHECK(state IN ('prepared','source_isolated','installed','released')),
        details jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
      );
      ALTER TABLE lead.usage_events ADD COLUMN details jsonb NOT NULL DEFAULT '{}';
      REVOKE ALL ON lead.proxy_pool,lead.proxy_assignments,lead.recovery_cases FROM PUBLIC;
      CREATE FUNCTION lead.claim_proxy(p_agent uuid) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
      DECLARE choice uuid; existing uuid;
      BEGIN
        IF NOT EXISTS(SELECT 1 FROM lead.agents WHERE id=p_agent AND tenant_id=lead.ctx_tenant() AND desired_state<>'archived' FOR UPDATE) THEN RETURN 'absent'; END IF;
        SELECT pool_id INTO existing FROM lead.proxy_assignments WHERE agent_id=p_agent AND released_at IS NULL;
        IF existing IS NOT NULL THEN RETURN 'assigned'; END IF;
        IF NOT EXISTS(SELECT 1 FROM lead.proxy_pool WHERE state<>'retired') THEN RETURN 'unconfigured'; END IF;
        SELECT p.id INTO choice FROM lead.proxy_pool p WHERE p.state='available' AND (p.expires_at IS NULL OR p.expires_at>now())
          AND NOT EXISTS(SELECT 1 FROM lead.proxy_assignments a WHERE a.pool_id=p.id AND a.released_at IS NULL)
          ORDER BY p.created_at FOR UPDATE OF p SKIP LOCKED LIMIT 1;
        IF choice IS NULL THEN RETURN 'queued'; END IF;
        INSERT INTO lead.proxy_assignments(pool_id,tenant_id,agent_id) VALUES(choice,lead.ctx_tenant(),p_agent);
        UPDATE lead.network_identities n SET provider_ref='managed',endpoint_ref=choice::text,secret_ref='managed:'||choice::text,mode='dedicated',expected_ip=p.expected_ip,expected_region=p.region,state='pending'
          FROM lead.proxy_pool p WHERE n.agent_id=p_agent AND n.tenant_id=lead.ctx_tenant() AND p.id=choice;
        RETURN 'assigned';
      END $$;
      CREATE FUNCTION lead.resolve_proxy(p_agent uuid) RETURNS TABLE(cipher bytea,expected_ip inet,region text,timezone text,locale text,usable boolean) LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
        SELECT p.cipher,p.expected_ip,p.region,p.timezone,p.locale,p.state='available' AND (p.expires_at IS NULL OR p.expires_at>now())
        FROM lead.proxy_pool p JOIN lead.proxy_assignments a ON a.pool_id=p.id WHERE a.agent_id=p_agent AND a.released_at IS NULL
      $$;
      CREATE FUNCTION lead.release_proxy(p_agent uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lead AS $$
      BEGIN
        IF EXISTS(SELECT 1 FROM lead.agents WHERE id=p_agent AND tenant_id=lead.ctx_tenant() AND desired_state='archived') THEN
          UPDATE lead.proxy_assignments SET released_at=now() WHERE agent_id=p_agent AND tenant_id=lead.ctx_tenant() AND released_at IS NULL;
          UPDATE lead.network_identities SET state='retired',valid_until=now() WHERE agent_id=p_agent AND tenant_id=lead.ctx_tenant();
        END IF;
      END $$;
      REVOKE EXECUTE ON FUNCTION lead.claim_proxy(uuid),lead.resolve_proxy(uuid),lead.release_proxy(uuid) FROM PUBLIC;
    """)


def downgrade():
    raise RuntimeError("Runtime identity and recovery history cannot be downgraded")
