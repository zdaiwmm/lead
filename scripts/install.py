"""Operator readiness, schema preview and verified local owner binding."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.installation import bind_owner, checks, schema_head


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["check", "schema-preview", "bind-owner"])
    p.add_argument("--tenant")
    p.add_argument("--issuer")
    p.add_argument("--subject")
    a = p.parse_args()
    if a.action == "check":
        result = checks()
    elif a.action == "schema-preview":
        import psycopg
        from lead_app.config import BROWSER_BUILD, database_kwargs

        with psycopg.connect(**database_kwargs(True)) as c:
            inventory = c.execute(
                "SELECT engine_version,sdk_version,count(*) FROM lead.browser_profiles GROUP BY engine_version,sdk_version ORDER BY engine_version,sdk_version"
            ).fetchall()
            adapters = c.execute(
                "SELECT platform,target_type,adapter_version,technical_state,permission_state FROM lead.platform_capabilities ORDER BY platform,target_type"
            ).fetchall()
            current = c.execute(
                "SELECT version_num FROM lead_migration_version"
            ).fetchone()[0]
        result = {
            "current": current,
            "target": schema_head(),
            "backup_required": current != schema_head(),
            "browser_build": BROWSER_BUILD,
            "installed_profiles": [
                {"build": v[0], "sdk": v[1], "count": v[2]} for v in inventory
            ],
            "adapters": [
                {
                    "platform": v[0],
                    "target_type": v[1],
                    "version": v[2],
                    "technical": v[3],
                    "permission": v[4],
                }
                for v in adapters
            ],
            "upgrade_rule": "snapshot_before_upgrade_and_reverify_changed_build_or_adapter",
            "rollback": "restore_into_new_fenced_database",
        }
    else:
        result = bind_owner(a.tenant, a.issuer, a.subject)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(
            json.dumps(
                {"error": str(e) if isinstance(e, ValueError) else type(e).__name__},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
