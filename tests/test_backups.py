import asyncio
import io
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from cryptography.exceptions import InvalidTag
from lead_app import backups, domain
from lead_app.backup_crypto import decrypt_to_file, encrypt_stream, key_init, load_key
from lead_app.browser import Actor, Gateway
from lead_app.config import STATE, TENANT, database_kwargs
from lead_app.db import one, tx, uid
from psycopg import sql
from psycopg.rows import dict_row


def test_authenticated_backup_rejects_tampering_truncation_and_wrong_key(tmp_path):
    key_file = tmp_path / "key"
    key_init(key_file)
    key = load_key(key_file)
    source = tmp_path / "sample.leadbackup"
    value = b"private-cookie-and-ledger\0" * 90000
    encrypt_stream(io.BytesIO(value), source, key, {"id": str(uuid4()), "kind": "test"})
    assert b"private-cookie" not in source.read_bytes()
    output = tmp_path / "plain"
    decrypt_to_file(source, output, key)
    assert output.read_bytes() == value
    original = source.read_bytes()
    for index, data in enumerate(
        (
            original[:-1],
            original[:-40],
            original[:-1] + bytes([original[-1] ^ 1]),
            original + b"extra",
        )
    ):
        bad = tmp_path / f"bad{index}"
        bad.write_bytes(data)
        destination = tmp_path / f"output{index}"
        with pytest.raises((ValueError, InvalidTag)):
            decrypt_to_file(bad, destination, key)
        assert (
            not destination.exists()
            and not destination.with_name(destination.name + ".partial").exists()
        )
    with pytest.raises(ValueError):
        decrypt_to_file(source, tmp_path / "wrong", os.urandom(32))
    key_file.chmod(0o644)
    with pytest.raises(ValueError):
        load_key(key_file)


def test_live_database_backup_restores_into_new_fenced_database(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    from lead_app import assets
    from PIL import Image
    from starlette.datastructures import UploadFile

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(buffer, "PNG")
    asset = asyncio.run(
        assets.upload(
            aid, UploadFile(file=io.BytesIO(buffer.getvalue()), filename="restore.png")
        )
    )
    _, asset_path = assets.resolve(aid, str(asset["id"]))
    original = asset_path.read_bytes()
    domain.connect_lab(aid)
    task = domain.queue_lab_publish(aid, "Preserved ledger intent", uid())
    from lead_app.activities import prepare_action

    action = prepare_action(aid, str(task["id"]))
    key = os.urandom(32)
    name = "lead_restore_test_" + uuid4().hex[:16]
    failed_name = "lead_restore_test_" + uuid4().hex[:16]
    record = None
    try:
        record = backups.backup_database(tmp_path, key)
        archive = tmp_path / (record["id"] + ".leadbackup")
        asset_path.unlink()  # Only this test's new object simulates data loss.
        result = backups.restore_database(archive, key, name)
        assert result["data_files_verified"] > 0 and asset_path.read_bytes() == original
        assert (
            result["external_writes"] == "blocked" and result["counts"]["actions"] >= 1
        )
        with psycopg.connect(
            **{**database_kwargs(True), "dbname": name}, row_factory=dict_row
        ) as c:
            c.execute(
                "SELECT set_config('app.tenant_id',%s,true),set_config('app.agent_id',%s,true)",
                (TENANT, aid),
            )
            copy = one(
                c, "SELECT intent,state FROM lead.actions WHERE id=%s", (action["id"],)
            )
            assert copy["intent"] == action["intent"] and copy["state"] == "prepared"
            with pytest.raises(ValueError, match="恢复数据库"):
                Actor(aid).gate(c, action)
        with pytest.raises(ValueError, match="已存在"):
            backups.restore_database(archive, key, name)
        with pytest.raises(ValueError):
            backups.restore_database(archive, key, "lead")
        # Restoring a database never rolls back the source action ledger.
        with tx(aid) as c:
            assert (
                one(c, "SELECT state FROM lead.actions WHERE id=%s", (action["id"],))[
                    "state"
                ]
                == "prepared"
            )
        from lead_app import backup_files

        def interrupted_install(*args):
            raise OSError("simulated file restore interruption")

        monkeypatch.setattr(backup_files, "install", interrupted_install)
        with pytest.raises(OSError):
            backups.restore_database(archive, key, failed_name)
        with psycopg.connect(**{**database_kwargs(True), "dbname": failed_name}) as c:
            assert (
                c.execute(
                    "SELECT state FROM lead.recovery_control WHERE singleton"
                ).fetchone()[0]
                == "recovery_required"
            )
            assert (
                c.execute(
                    "SELECT count(*) FROM lead.auth_sessions WHERE revoked_at IS NULL"
                ).fetchone()[0]
                == 0
            )
    finally:
        domain.set_control(aid, "archive")
        with psycopg.connect(**database_kwargs(True), autocommit=True) as c:
            for target in (name, failed_name):
                if c.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (target,)
                ).fetchone():
                    c.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(target)
                        )
                    )
            if record:
                c.execute(
                    "DELETE FROM lead.backup_records WHERE id=ANY(%s::uuid[])",
                    ([record["id"], record["files_snapshot_id"]],),
                )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="native profile safe-point backup",
)
async def test_real_profile_backup_keeps_storage_and_identity_and_ledger(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    gateway = Gateway()
    key = os.urandom(32)
    record = None
    quarantine = None

    def offline(*a, **k):
        raise httpx.ConnectError("isolated test gateway")

    monkeypatch.setattr(backups.httpx, "post", offline)
    try:
        actor = await gateway.get(aid)
        await actor.page.goto(f"http://127.0.0.1:8088/lab/{aid}/")
        await actor.page.evaluate(
            "localStorage.setItem('backup-proof','persisted-test-cookie')"
        )
        identity = (STATE / "profiles" / aid / "identity.json").read_bytes()
        with pytest.raises(ValueError, match="原浏览器仍在运行"):
            await asyncio.to_thread(backups.backup_profile, aid, tmp_path, key)
        await gateway.close(aid)
        with tx(aid) as c:
            version = one(c, "SELECT control_version FROM lead.agents")[
                "control_version"
            ]
        record = await asyncio.to_thread(backups.backup_profile, aid, tmp_path, key)
        result = await asyncio.to_thread(
            backups.restore_profile, tmp_path / (record["id"] + ".leadbackup"), key
        )
        quarantine = Path(result["path"])
        assert result["state"] == "quarantined" and not result["ledger_restored"]
        assert (quarantine / "identity.json").read_bytes() == identity
        assert list((quarantine / "storage").rglob("*.sqlite"))
        assert (quarantine / "RECOVERY_REQUIRED").exists()
        with tx(aid) as c:
            assert (
                one(c, "SELECT control_version FROM lead.agents")["control_version"]
                > version
            )
            assert (
                one(
                    c,
                    "SELECT count(*) AS n FROM lead.profile_maintenance WHERE state='active'",
                )["n"]
                == 0
            )
        reopened = await gateway.get(aid)
        await reopened.page.goto(f"http://127.0.0.1:8088/lab/{aid}/")
        assert (
            await reopened.page.evaluate("localStorage.getItem('backup-proof')")
            == "persisted-test-cookie"
        )
    finally:
        await gateway.close(aid)
        domain.set_control(aid, "archive")
        if quarantine:
            import shutil

            shutil.rmtree(quarantine)
        if record:
            with psycopg.connect(**database_kwargs(True)) as c:
                c.execute(
                    "DELETE FROM lead.backup_records WHERE id=%s", (record["id"],)
                )


def test_maintenance_blocks_launch_and_human_takeover(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    try:
        guard = backups.claim_profile_backup(aid)
        with pytest.raises(ValueError):
            backups.claim_profile_backup(aid)
        with tx(aid) as c:
            assert one(c, "SELECT id FROM lead.profile_maintenance")[
                "id"
            ] == __import__("uuid").UUID(guard)
    finally:
        domain.set_control(aid, "archive")


def test_database_and_file_list_share_one_snapshot(tmp_path, monkeypatch):
    from lead_app import backup_files

    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    observed = {}
    original = backup_files.referenced_files

    def concurrent_archive(connection=None):
        # This change commits after pg_export_snapshot, before references read.
        domain.set_control(aid, "archive")
        assert connection is not None
        observed["state"] = connection.execute(
            "SELECT desired_state FROM lead.agents WHERE id=%s", (aid,)
        ).fetchone()["desired_state"]
        return original(connection)

    monkeypatch.setattr(backup_files, "referenced_files", concurrent_archive)
    record = None
    try:
        record = backups.backup_database(tmp_path, os.urandom(32))
        assert observed["state"] == "draft"
        with tx(aid) as c:
            assert (
                one(c, "SELECT desired_state FROM lead.agents")["desired_state"]
                == "archived"
            )
    finally:
        if record:
            with psycopg.connect(**database_kwargs(True)) as c:
                c.execute(
                    "DELETE FROM lead.backup_records WHERE id=ANY(%s::uuid[])",
                    ([record["id"], record["files_snapshot_id"]],),
                )
