"""Only new disposable databases are fenced; never the user's source database."""

import json
import os
import shutil
from uuid import uuid4

import psycopg
import pytest
from lead_app import db, domain, recovery
from lead_app.backups import digest
from lead_app.config import BROWSER_BUILD, BROWSER_SDK, STATE, database_kwargs
from psycopg import sql

from scripts.migrate import upgrade


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_WORKFLOW_TESTS") != "1",
    reason="actual Temporal namespace cutover",
)
async def test_cutover_requires_fenced_source_reviews_and_installs_new_profile(
    monkeypatch,
):
    suffix = uuid4().hex[:16]
    source, target = "lead_test_cutover_" + suffix, "lead_restore_cutover_" + suffix
    node = STATE / "recovered-nodes" / suffix
    quarantine = STATE / "restore-quarantine" / str(uuid4())
    owner = database_kwargs(True)
    aid = None
    try:
        with psycopg.connect(**owner, autocommit=True) as c:
            c.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(source)
                )
            )
        upgrade({**owner, "dbname": source})
        runtime_kwargs = database_kwargs()
        monkeypatch.setattr(
            db, "database_kwargs", lambda: {**runtime_kwargs, "dbname": source}
        )
        monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
        aid = domain.create_agent()
        domain.connect_lab(aid)
        with recovery.connection(source) as c:
            p = c.execute(
                "SELECT * FROM lead.browser_profiles WHERE agent_id=%s", (aid,)
            ).fetchone()
            c.execute(
                "UPDATE lead.browser_profiles SET engine_version=%s,sdk_version=%s,state='stopped' WHERE agent_id=%s",
                (BROWSER_BUILD, BROWSER_SDK, aid),
            )
        with psycopg.connect(**owner, autocommit=True) as c:
            c.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(target), sql.Identifier(source)
                )
            )
        with recovery.connection(target) as c:
            c.execute(
                "UPDATE lead.recovery_control SET state='recovery_required',backup_id=%s,recovery_window_start=now() WHERE singleton",
                (str(uuid4()),),
            )
        item = recovery.prepare(target, source, node)
        identifier = item["id"]
        with pytest.raises(ValueError, match="尚未逐目标核对"):
            await recovery.release(target, identifier)
        assert recovery.isolate_source(target, identifier)["state"] == "source_isolated"
        with recovery.connection(source) as c:
            assert (
                c.execute("SELECT state FROM lead.recovery_control").fetchone()["state"]
                == "recovery_required"
            )
            assert (
                c.execute(
                    "SELECT desired_state FROM lead.agents WHERE id=%s", (aid,)
                ).fetchone()["desired_state"]
                == "paused"
            )
        quarantine.mkdir(parents=True)
        (quarantine / "RECOVERY_REQUIRED").write_text("quarantined")
        (quarantine / "identity.json").write_text('{"test":"owned-only"}')
        manifest = {
            "agent_id": aid,
            "tenant_id": str(p["tenant_id"]),
            "profile_id": str(p["id"]),
            "config_hash": p["config_hash"],
            "engine_version": BROWSER_BUILD,
            "sdk_version": BROWSER_SDK,
            "files": {
                "identity.json": {
                    "sha256": digest(quarantine / "identity.json"),
                    "size": (quarantine / "identity.json").stat().st_size,
                }
            },
        }
        (quarantine / "manifest.json").write_text(json.dumps(manifest))
        assert (
            recovery.install_profile(target, identifier, quarantine)["state"]
            == "installed"
        )
        assert (
            recovery.install_profile(target, identifier, quarantine)["state"]
            == "installed"
        )
        for tid in item["targets"]:
            recovery.review(
                target,
                identifier,
                tid,
                "No external submissions; owned test fixture only",
            )
        result = await recovery.release(target, identifier)
        assert result["agents"] == "paused" and result["authorization"] == "required"
        assert result["launch"]["LEAD_DB_NAME"] == target
        assert await recovery.release(target, identifier) == result
        with recovery.connection(target) as c:
            assert (
                c.execute("SELECT state FROM lead.recovery_control").fetchone()["state"]
                == "normal"
            )
            assert (
                c.execute(
                    "SELECT count(*) AS n FROM lead.authorization_grants WHERE revoked_at IS NULL"
                ).fetchone()["n"]
                == 0
            )
            profile = c.execute(
                "SELECT storage_ref FROM lead.browser_profiles WHERE agent_id=%s",
                (aid,),
            ).fetchone()
            assert str(node) in profile["storage_ref"]
        with recovery.connection(source) as c:
            assert (
                c.execute("SELECT state FROM lead.recovery_control").fetchone()["state"]
                == "recovery_required"
            )
    finally:
        monkeypatch.setattr(db, "database_kwargs", database_kwargs)
        with psycopg.connect(**owner, autocommit=True) as c:
            for name in (target, source):
                if c.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
                ).fetchone():
                    c.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(name)
                        )
                    )
        for path in (node, quarantine):
            if path.exists():
                shutil.rmtree(path)
