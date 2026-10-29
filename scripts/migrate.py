"""Idempotent local install. Runtime uses a separate non-owner, NOBYPASSRLS role."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
import psycopg
from lead_app.config import CONFIG, ROOT, TENANT, database_kwargs
from psycopg import sql

with psycopg.connect(**database_kwargs(True), autocommit=True) as conn:
    if not conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name='lead'"
    ).fetchone():
        conn.execute((ROOT / "infra/schema.sql").read_text())
    conn.execute((ROOT / "infra/runtime.sql").read_text())
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
    conn.execute(
        "REVOKE INSERT,UPDATE,DELETE ON lead.platform_capabilities FROM lead_runtime"
    )
    conn.execute("REVOKE UPDATE,DELETE ON lead.audit_events FROM lead_runtime")
    conn.execute("REVOKE DELETE ON ALL TABLES IN SCHEMA lead FROM lead_runtime")
    conn.execute(
        "REVOKE UPDATE ON lead.agent_revisions,lead.business_profiles,lead.content_items FROM lead_runtime"
    )
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
print("Database schema and scoped runtime role ready.")
