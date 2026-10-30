"""Operator-only role and capability provisioning, separate from schema upgrades."""

from psycopg import sql

from .config import CONFIG, TENANT


def provision_security(conn):
    if not conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname='lead_identity_owner'"
    ).fetchone():
        conn.execute(
            "CREATE ROLE lead_identity_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
        )
    conn.execute("GRANT USAGE ON SCHEMA lead TO lead_identity_owner")
    tables = [
        "tenants",
        "memberships",
        "auth_sessions",
        "oauth_transactions",
        "agents",
        "tracking_links",
        "analytics_connections",
        "analytics_credentials",
        "audit_events",
    ]
    for table in tables:
        conn.execute(
            sql.SQL("GRANT SELECT ON lead.{} TO lead_identity_owner").format(
                sql.Identifier(table)
            )
        )
        if not conn.execute(
            "SELECT 1 FROM pg_policies WHERE schemaname='lead' AND tablename=%s AND policyname='identity_resolver'",
            (table,),
        ).fetchone():
            conn.execute(
                sql.SQL(
                    "CREATE POLICY identity_resolver ON lead.{} TO lead_identity_owner USING(true) WITH CHECK(true)"
                ).format(sql.Identifier(table))
            )
    conn.execute(
        "GRANT INSERT,UPDATE ON lead.tenants,lead.memberships,lead.auth_sessions,lead.oauth_transactions TO lead_identity_owner"
    )
    conn.execute("GRANT INSERT ON lead.audit_events TO lead_identity_owner")
    functions = [
        "auth_enroll(text,text)",
        "auth_create_session(text,uuid,text,text)",
        "auth_session(text)",
        "auth_logout(text)",
        "auth_begin(text,text,bytea)",
        "auth_consume(text,text)",
        "auth_members(text)",
        "auth_member_change(text,text,text,text,text)",
        "auth_workspaces(text)",
        "auth_switch(text,uuid)",
        "execution_agent_tenant(uuid)",
        "execution_tenants()",
        "public_ref_tenant(text,text)",
    ]
    for function in functions:
        conn.execute(
            sql.SQL("REVOKE EXECUTE ON FUNCTION lead." + function + " FROM PUBLIC")
        )
        conn.execute(
            sql.SQL("ALTER FUNCTION lead." + function + " OWNER TO lead_identity_owner")
        )
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='lead_auth'").fetchone():
        conn.execute(
            sql.SQL(
                "CREATE ROLE lead_auth LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            ).format(sql.Literal(CONFIG["auth_password"]))
        )
    conn.execute("GRANT USAGE ON SCHEMA lead TO lead_auth")
    conn.execute(
        "GRANT EXECUTE ON FUNCTION lead.auth_enroll(text,text),lead.auth_create_session(text,uuid,text,text),lead.auth_session(text),lead.auth_logout(text),lead.auth_begin(text,text,bytea),lead.auth_consume(text,text),lead.auth_members(text),lead.auth_member_change(text,text,text,text,text),lead.auth_workspaces(text),lead.auth_switch(text,uuid),lead.execution_agent_tenant(uuid),lead.execution_tenants(),lead.public_ref_tenant(text,text) TO lead_auth"
    )
    if not conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname='lead_runtime'"
    ).fetchone():
        conn.execute(
            sql.SQL(
                "CREATE ROLE lead_runtime LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            ).format(sql.Literal(CONFIG["runtime_password"]))
        )
    conn.execute("GRANT USAGE ON SCHEMA lead TO lead_runtime")
    conn.execute(
        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA lead TO lead_runtime"
    )
    conn.execute("REVOKE ALL ON lead.schema_versions FROM lead_runtime")
    conn.execute("REVOKE ALL ON lead.ops_alerts FROM lead_runtime")
    conn.execute(
        "REVOKE EXECUTE ON FUNCTION lead.backup_ready(),lead.offsite_backup_ready(),lead.confirm_channel_probe(uuid),lead.confirm_telegram_probe(uuid) FROM PUBLIC"
    )
    conn.execute(
        "GRANT EXECUTE ON FUNCTION lead.backup_ready(),lead.offsite_backup_ready(),lead.confirm_channel_probe(uuid),lead.confirm_telegram_probe(uuid) TO lead_runtime"
    )
    conn.execute(
        "REVOKE ALL ON lead.auth_sessions,lead.oauth_transactions FROM lead_runtime"
    )
    conn.execute(
        "REVOKE INSERT,UPDATE,DELETE ON lead.memberships,lead.tenants FROM lead_runtime"
    )
    conn.execute(
        "REVOKE ALL ON lead.recovery_control,lead.backup_records FROM lead_runtime"
    )
    conn.execute("GRANT SELECT ON lead.recovery_control TO lead_runtime")
    conn.execute(
        "REVOKE INSERT,UPDATE,DELETE ON lead.platform_capabilities FROM lead_runtime"
    )
    conn.execute("REVOKE UPDATE,DELETE ON lead.audit_events FROM lead_runtime")
    conn.execute("REVOKE UPDATE,DELETE ON lead.admin_config_versions FROM lead_runtime")
    conn.execute(
        "REVOKE INSERT,UPDATE,DELETE ON lead.recovery_observations FROM lead_runtime"
    )
    conn.execute("REVOKE DELETE ON ALL TABLES IN SCHEMA lead FROM lead_runtime")
    conn.execute(
        "REVOKE UPDATE ON lead.agent_revisions,lead.business_profiles,lead.content_items FROM lead_runtime"
    )

    # Managed endpoint credentials are readable only through the service resolver.
    conn.execute(
        "GRANT SELECT,INSERT,UPDATE ON lead.proxy_pool,lead.proxy_assignments TO lead_identity_owner"
    )
    conn.execute("GRANT UPDATE(id) ON lead.agents TO lead_identity_owner")
    conn.execute("GRANT UPDATE ON lead.network_identities TO lead_identity_owner")
    conn.execute("GRANT SELECT ON lead.network_identities TO lead_identity_owner")
    if not conn.execute(
        "SELECT 1 FROM pg_policies WHERE schemaname='lead' AND tablename='network_identities' AND policyname='identity_resolver'"
    ).fetchone():
        conn.execute(
            "CREATE POLICY identity_resolver ON lead.network_identities TO lead_identity_owner USING(true) WITH CHECK(true)"
        )
    for function in ("claim_proxy(uuid)", "resolve_proxy(uuid)", "release_proxy(uuid)"):
        conn.execute(
            sql.SQL("ALTER FUNCTION lead." + function + " OWNER TO lead_identity_owner")
        )
        conn.execute(
            sql.SQL("REVOKE EXECUTE ON FUNCTION lead." + function + " FROM PUBLIC")
        )
    conn.execute(
        "GRANT EXECUTE ON FUNCTION lead.claim_proxy(uuid),lead.release_proxy(uuid) TO lead_runtime"
    )
    conn.execute("GRANT EXECUTE ON FUNCTION lead.resolve_proxy(uuid) TO lead_auth")
    conn.execute(
        "REVOKE ALL ON lead.proxy_pool,lead.proxy_assignments,lead.recovery_cases FROM lead_runtime,lead_auth"
    )


def provision(conn):
    provision_security(conn)
    conn.execute(
        "INSERT INTO lead.tenants(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING",
        (TENANT, "lead 本地工作区"),
    )
    for platform in [
        "telegram",
        "facebook",
        "instagram",
        "youtube",
        "tiktok",
        "x",
        "linkedin",
        "reddit",
        "pinterest",
        "threads",
        "whatsapp",
        "lab",
    ]:
        conn.execute(
            "INSERT INTO lead.platform_capabilities(platform,action_type,target_type,technical_state,permission_state,adapter_version) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                platform,
                "publish",
                "channel" if platform == "telegram" else "profile",
                "verified" if platform == "lab" else "unverified",
                "approved" if platform == "lab" else "pending",
                "local-v1" if platform == "lab" else "browser-v1",
            ),
        )
    conn.execute(
        "UPDATE lead.platform_capabilities SET permission_state='approved',adapter_version=CASE WHEN technical_state='verified' THEN adapter_version ELSE 'telegram-webk-2' END,evidence_ref=CASE WHEN technical_state='verified' THEN evidence_ref ELSE 'docs/policies/telegram-owned-channel.md' END,reviewed_at=now() WHERE platform='telegram' AND action_type='publish' AND target_type='channel'"
    )
    from lead_app.adapters.catalog import SPECS

    for spec in SPECS.values():
        for target_type in (
            ["profile", "page"] if spec.platform == "linkedin" else [spec.target_type]
        ):
            conn.execute(
                "INSERT INTO lead.platform_capabilities(platform,action_type,target_type,technical_state,permission_state,adapter_version) VALUES(%s,'publish',%s,'unverified',%s,%s) ON CONFLICT(platform,action_type,target_type) DO UPDATE SET adapter_version=excluded.adapter_version,technical_state=CASE WHEN lead.platform_capabilities.adapter_version=excluded.adapter_version THEN lead.platform_capabilities.technical_state ELSE 'unverified' END,permission_state=CASE WHEN excluded.permission_state='restricted' THEN 'restricted' ELSE lead.platform_capabilities.permission_state END",
                (
                    spec.platform,
                    target_type,
                    "approved" if spec.platform == "telegram" else spec.policy,
                    spec.version,
                ),
            )
