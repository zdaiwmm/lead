"""Read-only readiness and explicit local-workspace ownership migration."""

import json
import os
import platform
import stat
import subprocess
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import (
    BROWSER_BUILD,
    BROWSER_SDK,
    ROOT,
    STATE,
    TENANT,
    database_kwargs,
    env,
    model_config,
)


def storage_encryption():
    """Positive OS evidence for a mounted Linux dm-crypt volume; else unknown."""
    if platform.system() != "Linux":
        return None
    try:
        mounted = subprocess.run(
            ["findmnt", "--json", "--target", str(STATE), "--output", "SOURCE"],
            capture_output=True,
            check=True,
            timeout=5,
            text=True,
        )
        source = json.loads(mounted.stdout)["filesystems"][0]["source"].split("[", 1)[0]
        if not source.startswith("/dev/"):
            return None
        devices = subprocess.run(
            ["lsblk", "--inverse", "--json", "--output", "NAME,TYPE", source],
            capture_output=True,
            check=True,
            timeout=5,
            text=True,
        )

        def encrypted(rows):
            return any(
                row.get("type") == "crypt" or encrypted(row.get("children", []))
                for row in rows
            )

        return True if encrypted(json.loads(devices.stdout)["blockdevices"]) else None
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return None


def checks():
    values = env()
    result = {"mode": values.get("LEAD_DEPLOYMENT_MODE") or "local", "checks": []}

    def add(name, passed, detail=None):
        result["checks"].append(
            {
                "name": name,
                "state": "passed"
                if passed is True
                else "failed"
                if passed is False
                else "external_verification_required",
                "detail": detail,
            }
        )

    add("model_key", bool(model_config()["key"]))
    for name, path in (
        ("local_configuration", STATE / "config.json"),
        ("model_environment", ROOT / ".env"),
    ):
        add(
            name,
            path.is_file()
            and not path.is_symlink()
            and not (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)),
        )
    try:
        with psycopg.connect(**database_kwargs(), row_factory=dict_row) as c:
            role = c.execute(
                "SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            add("runtime_role", not any(role.values()))
            add(
                "recovery_fence",
                c.execute("SELECT state FROM lead.recovery_control").fetchone()["state"]
                == "normal",
            )
        with psycopg.connect(**database_kwargs(True), row_factory=dict_row) as c:
            add(
                "database_schema",
                c.execute("SELECT version_num FROM lead_migration_version").fetchone()[
                    "version_num"
                ]
                == schema_head(),
            )
    except Exception as e:
        add("database", False, type(e).__name__)
    try:
        from importlib.metadata import version

        from camoufox.multiversion import find_installed_version
        from camoufox.pkgman import launch_path

        add("browser_sdk", version("camoufox") == BROWSER_SDK)
        # This identifies the installed pinned build; it does not launch it.
        installed = find_installed_version(BROWSER_BUILD)
        add(
            "browser_build",
            bool(installed and Path(launch_path(installed)).is_file()),
            BROWSER_BUILD,
        )
    except Exception as e:
        add("browser_installation", False, type(e).__name__)
    production = result["mode"] == "production"
    if production:
        add("ordinary_system_user", os.geteuid() != 0)
        add(
            "oidc",
            values.get("LEAD_AUTH_MODE") == "oidc"
            and all(
                values.get(k)
                for k in (
                    "LEAD_OIDC_ISSUER",
                    "LEAD_OIDC_CLIENT_ID",
                    "LEAD_OIDC_CLIENT_SECRET",
                )
            ),
        )
        add("https", (values.get("LEAD_PUBLIC_URL") or "").startswith("https://"))
        add("database_tls", values.get("LEAD_DB_SSLMODE") == "verify-full")
        add(
            "namespace_host",
            platform.system() == "Linux"
            and values.get("LEAD_NETWORK_NAMESPACE") == "1",
        )
        add("offsite_backup", bool(values.get("LEAD_BACKUP_S3_URI")))
        key = Path(
            values.get("LEAD_BACKUP_KEY_FILE") or STATE / "keys/backup.key"
        ).resolve()
        add(
            "independent_backup_key",
            key.is_file()
            and not key.is_relative_to(ROOT)
            and not (key.stat().st_mode & 0o077),
        )
        # An operator assertion is not evidence that the block device is encrypted.
        add(
            "active_profile_storage_encryption",
            storage_encryption(),
            "Verify the actual mounted device and key recovery on the target host",
        )
    else:
        add(
            "local_interfaces",
            (values.get("LEAD_PUBLIC_URL") or "http://127.0.0.1:3100").rstrip("/")
            in ("http://127.0.0.1:3100", "http://localhost:3100"),
        )
    result["ready"] = all(c["state"] == "passed" for c in result["checks"])
    return result


def schema_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(
        Config(str(ROOT / "infra/alembic.ini"))
    ).get_current_head()


def bind_owner(tenant, issuer, subject):
    """Only a still-valid, previously OIDC-verified principal can receive ownership."""
    from uuid import UUID

    tenant = str(UUID(tenant))
    if issuer == "local" or not issuer.startswith("https://") or not subject:
        raise ValueError("需要已通过 OIDC 验证的身份")
    with psycopg.connect(**database_kwargs(True), row_factory=dict_row) as c:
        proof = c.execute(
            "SELECT 1 FROM lead.auth_sessions s JOIN lead.memberships m ON m.tenant_id=s.tenant_id AND m.oidc_issuer=s.issuer AND m.oidc_subject=s.subject WHERE s.issuer=%s AND s.subject=%s AND s.revoked_at IS NULL AND s.expires_at>now() AND m.status='active'",
            (issuer, subject),
        ).fetchone()
        if not proof:
            raise ValueError("此身份没有有效的已验证登录会话，请先完成登录")
        workspace = c.execute(
            "SELECT id FROM lead.tenants WHERE id=%s FOR UPDATE", (tenant,)
        ).fetchone()
        if not workspace:
            raise ValueError("工作室不存在")
        c.execute(
            "INSERT INTO lead.memberships(tenant_id,oidc_issuer,oidc_subject,role,status) VALUES(%s,%s,%s,'owner','active') ON CONFLICT(tenant_id,oidc_issuer,oidc_subject) DO UPDATE SET role='owner',status='active'",
            (tenant, issuer, subject),
        )
        c.execute(
            "INSERT INTO lead.audit_events(tenant_id,actor_type,actor_ref,type,object_ref,payload,trace_id) VALUES(%s,'operator','local-console','workspace.owner.bound',%s,%s::jsonb,gen_random_uuid()::text)",
            (tenant, tenant, json.dumps({"issuer": issuer, "subject": subject})),
        )
    return {
        "tenant_id": tenant,
        "owner_bound": True,
        "local_workspace": tenant == TENANT,
    }
