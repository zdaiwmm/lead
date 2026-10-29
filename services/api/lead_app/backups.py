"""Operator backup/recovery. Restore never overwrites a running database/profile."""

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg import sql

from .backup_crypto import EncryptedWriter, decrypt_to_file, encrypt_stream, key_id
from .config import ROOT, STATE, database_kwargs, env
from .db import one, require_agent, tx
from .resources import ensure_capacity
from .security import internal_token


def now():
    return datetime.now(timezone.utc).isoformat()


class BackupBusy(ValueError):
    code = "backup_busy"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pg_command(tool, database):
    values = env()
    explicit = values.get("LEAD_" + tool.upper() + "_BIN")
    executable = explicit or shutil.which(tool)
    if executable and not explicit:
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=5
        )
        found = re.search(r"\b(\d+)\.", version.stdout)
        with psycopg.connect(**database_kwargs(True)) as c:
            major = int(c.execute("SHOW server_version_num").fetchone()[0]) // 10000
        if version.returncode or not found or int(found[1]) != major:
            executable = None
    if executable:
        kwargs = database_kwargs(True)
        process_env = {
            **os.environ,
            "PGHOST": kwargs["host"],
            "PGPORT": str(kwargs["port"]),
            "PGUSER": kwargs["user"],
            "PGPASSWORD": kwargs["password"],
            "PGSSLMODE": kwargs["sslmode"],
        }
        if kwargs.get("sslrootcert"):
            process_env["PGSSLROOTCERT"] = kwargs["sslrootcert"]
        return [executable, "--dbname", database], process_env
    if values.get("LEAD_DB_HOST") not in (None, "", "127.0.0.1", "localhost"):
        raise ValueError("外部数据库需要配置匹配版本的 PostgreSQL 客户端")
    return [
        "docker",
        "compose",
        "--env-file",
        str(STATE / "infra.env"),
        "-f",
        str(ROOT / "infra/compose.yaml"),
        "exec",
        "-T",
        "postgres",
        tool,
        "--username",
        "lead_owner",
        "--dbname",
        database,
    ], dict(os.environ)


@contextmanager
def staging():
    folder = STATE / "recovery-staging"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=folder) as name:
        os.chmod(name, 0o700)
        yield Path(name)


def catalog(path, metadata, key):
    result = {
        **metadata,
        "completed_at": now(),
        "cipher_sha256": digest(path),
        "size_bytes": Path(path).stat().st_size,
        "key_id": key_id(key),
    }
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.backup_records(id,kind,agent_id,started_at,completed_at,cipher_sha256,key_id,size_bytes,object_ref,manifest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                result["id"],
                result["kind"],
                result.get("agent_id"),
                result["started_at"],
                result["completed_at"],
                result["cipher_sha256"],
                result["key_id"],
                result["size_bytes"],
                str(Path(path).resolve()),
                psycopg.types.json.Jsonb(result),
            ),
        )
    return result


def destination(folder, identifier):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink():
        raise ValueError("备份目录不能是链接")
    return folder / (identifier + ".leadbackup")


@contextmanager
def backup_lock(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / ".backup.lock").open("a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupBusy("另一项备份操作尚未结束") from None
        yield


def backup_database(folder, key, managed=False):
    with backup_lock(folder):
        return _backup_database(folder, key, managed)


def _backup_database(folder, key, managed):
    ensure_capacity(256)
    identifier = str(uuid4())
    database = database_kwargs(True)["dbname"]
    metadata = {
        "id": identifier,
        "kind": "database",
        "started_at": now(),
        "source_database": database,
        "files_snapshot_id": str(uuid4()),
        "managed": managed,
    }
    path = destination(folder, identifier)
    command, process_env = pg_command("pg_dump", database)
    command += [
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--lock-wait-timeout=10000",
    ]
    from psycopg.rows import dict_row

    from .backup_files import referenced_files

    with psycopg.connect(**database_kwargs(True), row_factory=dict_row) as snapshot:
        snapshot.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot_id = snapshot.execute("SELECT pg_export_snapshot() AS id").fetchone()[
            "id"
        ]
        command += ["--snapshot", snapshot_id]
        references = referenced_files(snapshot)
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                env=process_env,
                stdout=subprocess.PIPE,
                stderr=errors,
                cwd=ROOT,
            )
            watchdog = threading.Timer(300, process.kill)
            watchdog.start()
            try:
                encrypt_stream(process.stdout, path, key, metadata)
                process.wait(timeout=10)
                if process.returncode:
                    path.unlink(missing_ok=True)
                    raise RuntimeError("数据库备份未完成")
            except BaseException:
                process.kill()
                process.wait()
                path.unlink(missing_ok=True)
                raise
            finally:
                watchdog.cancel()
                process.stdout.close()
    from .backup_files import capture

    try:
        files = capture(
            folder,
            key,
            metadata["files_snapshot_id"],
            managed=managed,
            references=references,
        )
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return catalog(
        path, {**metadata, "files_snapshot_sha256": files["cipher_sha256"]}, key
    )


EXCLUDE_PARTS = {
    "cache2",
    "startupCache",
    "thumbnails",
    "shader-cache",
    "crashes",
    "minidumps",
}
EXCLUDE_FILES = {"actor.lock", "parent.lock", ".parentlock", "lock"}


def profile_files(folder):
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if (
            any(part in EXCLUDE_PARTS for part in relative.parts)
            or path.name in EXCLUDE_FILES
        ):
            continue
        if path.is_symlink():
            raise ValueError("Profile 包含不允许备份的链接")
        if path.is_file() and (
            relative.parts[0] == "storage" or relative.as_posix() == "identity.json"
        ):
            yield path, relative.as_posix()


def claim_profile_backup(aid, allow_running=False):
    guard_id = str(uuid4())
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        if (agent["desired_state"] == "running" and not allow_running) or one(
            c,
            "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now()",
        ):
            raise ValueError("环境仍在工作或接管中，未开始备份")
        if one(
            c,
            "SELECT 1 FROM lead.tasks WHERE state IN ('running','queued','reconciling')",
        ) or one(c, "SELECT 1 FROM lead.chat_jobs WHERE status='processing'"):
            raise ValueError("环境尚未到达可备份的安全点")
        c.execute(
            "UPDATE lead.profile_maintenance SET state='complete' WHERE state='active' AND expires_at<=now()"
        )
        if one(c, "SELECT 1 FROM lead.profile_maintenance WHERE state='active'"):
            raise ValueError("此环境正在备份")
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        c.execute(
            "INSERT INTO lead.profile_maintenance(id,tenant_id,agent_id,profile_id,purpose,expires_at,state) VALUES(%s,%s,%s,%s,'backup',now()+interval '15 minutes','active')",
            (guard_id, agent["tenant_id"], aid, profile["id"]),
        )
        c.execute(
            "UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s",
            (aid,),
        )
    return guard_id


def backup_profile(aid, folder, key, automated=False):
    from .scope import service_scope

    with service_scope(), backup_lock(folder):
        return _backup_profile_request(aid, folder, key, automated)


def _backup_profile_request(aid, folder, key, automated):
    aid = str(UUID(aid))
    try:
        response = httpx.post(
            f"http://127.0.0.1:8089/agents/{aid}/backup/begin",
            headers={"Authorization": "Bearer " + internal_token()},
            timeout=45,
            trust_env=False,
            json={"automated": automated},
        )
        response.raise_for_status()
        guard_id = response.json()["guard_id"]
    except httpx.ConnectError:
        guard_id = claim_profile_backup(aid, allow_running=automated)
    try:
        return _backup_profile(aid, folder, key, automated)
    finally:
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.profile_maintenance SET state='complete' WHERE id=%s",
                (guard_id,),
            )
            if automated:
                from .db import emit

                emit(c, aid, "supervisor.wake")


def _backup_profile(aid, folder, key, automated=False):
    aid = str(UUID(aid))
    ensure_capacity(256)
    with tx(aid) as c:
        agent = require_agent(c, aid)
        if (agent["desired_state"] == "running" and not automated) or one(
            c,
            "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now()",
        ):
            raise ValueError("需要先停止此环境的工作和接管，才能备份登录资料")
    root = STATE / "profiles" / aid
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Profile 尚未创建")
    with (root / "actor.lock").open("a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("原浏览器仍在运行，拒绝复制登录资料") from None
        with tx(aid) as c:
            agent = require_agent(c, aid, True)
            profile = one(c, "SELECT * FROM lead.browser_profiles")
            if agent["desired_state"] == "running" and not automated:
                raise ValueError("环境状态已改变，未备份")
            files = list(profile_files(root))
            if not any(name == "identity.json" for _, name in files):
                raise ValueError("Profile 缺少设备身份文件")
            manifest = {
                "agent_id": aid,
                "tenant_id": str(agent["tenant_id"]),
                "profile_id": str(profile["id"]),
                "config_hash": profile["config_hash"],
                "engine_version": profile["engine_version"],
                "sdk_version": profile["sdk_version"],
                "files": {
                    name: {"sha256": digest(path), "size": path.stat().st_size}
                    for path, name in files
                },
            }
            metadata = {
                "id": str(uuid4()),
                "kind": "profile",
                "agent_id": aid,
                "started_at": now(),
                "managed": automated,
            }
            path = destination(folder, metadata["id"])
            writer = EncryptedWriter(path, key, metadata)
            try:
                with tarfile.open(fileobj=writer, mode="w|") as tar:
                    encoded = json.dumps(manifest, sort_keys=True).encode()
                    info = tarfile.TarInfo("manifest.json")
                    info.size = len(encoded)
                    info.mode = 0o600
                    tar.addfile(info, io.BytesIO(encoded))
                    for source, name in files:
                        info = tar.gettarinfo(str(source), arcname=name)
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mode = 0o600
                        with source.open("rb") as stream:
                            tar.addfile(info, stream)
                writer.finish()
            except BaseException:
                writer.abort()
                raise
    return catalog(path, metadata, key)


def restore_database(source, key, name):
    if (
        not re.fullmatch(r"lead_restore_[a-z0-9_]{1,48}", name)
        or name == database_kwargs(True)["dbname"]
    ):
        raise ValueError("只允许恢复到新建的 lead_restore_ 数据库，不能覆盖现有业务")
    ensure_capacity(512)
    started = time.monotonic()
    kwargs = database_kwargs(True)
    with staging() as folder:
        plain = folder / "database.dump"
        metadata = decrypt_to_file(source, plain, key)
        if metadata.get("kind") != "database":
            raise ValueError("备份不是数据库")
        files_manifest = None
        files = {}
        if metadata.get("files_snapshot_id"):
            from .backup_files import restore

            files_id = str(UUID(metadata["files_snapshot_id"]))
            files_manifest, files = restore(
                Path(source).parent / (files_id + ".leadbackup"), key, files_id, folder
            )
        with psycopg.connect(**kwargs, autocommit=True) as c:
            if c.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
            ).fetchone():
                raise ValueError("目标数据库已存在，拒绝覆盖")
            c.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(name)
                )
            )
        try:
            command, process_env = pg_command("pg_restore", name)
            with plain.open("rb") as stream:
                result = subprocess.run(
                    command
                    + [
                        "--no-owner",
                        "--no-privileges",
                        "--exit-on-error",
                        "--single-transaction",
                    ],
                    env=process_env,
                    stdin=stream,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=300,
                    cwd=ROOT,
                )
            if result.returncode:
                raise RuntimeError("数据库恢复未完成")
            with psycopg.connect(**{**kwargs, "dbname": name}) as c:
                c.execute(
                    "UPDATE lead.recovery_control SET state='recovery_required',backup_id=%s,restored_at=now(),recovery_window_start=%s,updated_at=now() WHERE singleton",
                    (metadata["id"], metadata["started_at"]),
                )
                c.commit()  # Later file/session errors cannot undo the fence.
                c.execute(
                    "UPDATE lead.auth_sessions SET revoked_at=now() WHERE revoked_at IS NULL"
                )
                c.execute(
                    "UPDATE lead.oauth_transactions SET used_at=now() WHERE used_at IS NULL"
                )
                c.commit()
                from .provision import provision_security

                provision_security(c)
                c.commit()
                if files_manifest:
                    from .backup_files import install

                    install(files_manifest, files, c)
                # Existing dispatches are retained exactly. The global fence is
                # effective before a worker can attach to this new database.
                counts = {
                    table: c.execute(
                        sql.SQL("SELECT count(*) FROM lead.{}").format(
                            sql.Identifier(table)
                        )
                    ).fetchone()[0]
                    for table in (
                        "agents",
                        "actions",
                        "conversation_messages",
                        "assets",
                    )
                }
        except BaseException:
            # Do not automatically drop a failed restore: retain it fenced for
            # diagnosis, with no service connection ever pointed at it.
            raise
    return {
        "ok": True,
        "database": name,
        "backup_id": metadata["id"],
        "external_writes": "blocked",
        "counts": counts,
        "data_files_verified": len(files),
        "restore_seconds": round(time.monotonic() - started, 3),
    }


def restore_profile(source, key):
    ensure_capacity(512)
    with staging() as folder:
        plain = folder / "profile.tar"
        metadata = decrypt_to_file(source, plain, key, maximum=4 * 1024**3)
        if metadata.get("kind") != "profile":
            raise ValueError("备份不是 Profile")
        output = STATE / "restore-quarantine" / str(UUID(metadata["id"]))
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.mkdir(mode=0o700)
        try:
            total = 0
            count = 0
            with tarfile.open(plain, "r:") as tar:
                for member in tar:
                    relative = PurePosixPath(member.name)
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or not member.isfile()
                        or len(relative.parts) > 40
                    ):
                        raise ValueError("Profile 归档路径不可用")
                    if relative.parts[0] not in (
                        "manifest.json",
                        "identity.json",
                        "storage",
                    ):
                        raise ValueError("Profile 归档包含额外文件")
                    count += 1
                    total += member.size
                    if count > 100000 or total > 4 * 1024**3:
                        raise ValueError("Profile 归档超出容量限制")
                    target = output.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with target.open("xb") as stream:
                        os.chmod(target, 0o600)
                        shutil.copyfileobj(
                            tar.extractfile(member), stream, length=1024**2
                        )
            manifest = json.loads((output / "manifest.json").read_text())
            if str(UUID(manifest["agent_id"])) != metadata["agent_id"]:
                raise ValueError("Profile 所属 Agent 不一致")
            actual = {
                p.relative_to(output).as_posix(): p
                for p in output.rglob("*")
                if p.is_file() and p.name != "manifest.json"
            }
            if set(actual) != set(manifest["files"]):
                raise ValueError("Profile 文件清单不一致")
            for name, path in actual.items():
                expected = manifest["files"][name]
                if (
                    path.stat().st_size != expected["size"]
                    or digest(path) != expected["sha256"]
                ):
                    raise ValueError("Profile 文件哈希不一致")
            (output / "RECOVERY_REQUIRED").write_text(
                "Quarantined. Do not launch before account and ledger reconciliation.\n"
            )
        except BaseException:
            shutil.rmtree(output)
            raise
    return {
        "ok": True,
        "path": str(output),
        "agent_id": metadata["agent_id"],
        "state": "quarantined",
        "ledger_restored": False,
        "files": len(actual),
    }


def prune_managed(folder, maximum_bytes=1024**3):
    with backup_lock(folder):
        return _prune_managed(folder, maximum_bytes)


def _prune_managed(folder, maximum_bytes):
    """Expire only daemon-owned backup IDs in its exact directory, never manual copies."""
    folder = Path(folder).resolve()
    with psycopg.connect(
        **database_kwargs(True),
        row_factory=__import__("psycopg.rows", fromlist=["dict_row"]).dict_row,
    ) as c:
        rows = c.execute(
            "SELECT * FROM lead.backup_records WHERE state='verified' ORDER BY started_at DESC"
        ).fetchall()
        managed = [
            r
            for r in rows
            if r["manifest"].get("managed") is True
            and Path(r["object_ref"]).parent.resolve() == folder
            and Path(r["object_ref"]).name == str(r["id"]) + ".leadbackup"
        ]
        databases = [r for r in managed if r["kind"] == "database"]
        keep = {r["id"] for r in databases[:3]}
        by_id = {str(r["id"]): r for r in rows}
        used = sum(
            r["size_bytes"]
            + by_id.get(r["manifest"].get("files_snapshot_id"), {}).get("size_bytes", 0)
            for r in databases[:3]
        )
        days = {r["started_at"].date() for r in databases[:3]}
        for row in databases[3:]:
            day = row["started_at"].date()
            size = row["size_bytes"] + by_id.get(
                row["manifest"].get("files_snapshot_id"), {}
            ).get("size_bytes", 0)
            if day not in days and len(days) < 7 and used + size <= maximum_bytes:
                keep.add(row["id"])
                days.add(day)
                used += size
        for row in managed:
            if row["kind"] == "profile":
                key = (
                    row["agent_id"],
                    sum(
                        1
                        for k in keep
                        if by_id.get(str(k), {}).get("agent_id") == row["agent_id"]
                    ),
                )
                if key[1] < 2:
                    keep.add(row["id"])
        # Every retained database keeps its paired material archive.
        for row in rows:
            if row["kind"] == "database" and (row["id"] in keep or row not in managed):
                pair = row["manifest"].get("files_snapshot_id")
                if pair in by_id:
                    keep.add(by_id[pair]["id"])
        expired = []
        for row in managed:
            if row["id"] in keep:
                continue
            path = Path(row["object_ref"])
            if path.is_symlink():
                continue
            path.unlink(missing_ok=True)
            c.execute(
                "UPDATE lead.backup_records SET state='expired' WHERE id=%s",
                (row["id"],),
            )
            expired.append(str(row["id"]))
    return expired


def send_offsite(record, folder, uri):
    from .backup_store import upload

    identifiers = (
        [record["files_snapshot_id"]] if record.get("files_snapshot_id") else []
    )
    identifiers.append(record["id"])
    for identifier in identifiers:
        remote = upload(Path(folder) / (identifier + ".leadbackup"), uri)
        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute(
                "UPDATE lead.backup_records SET manifest=manifest || %s WHERE id=%s",
                (psycopg.types.json.Jsonb({"remote": remote}), identifier),
            )
    return remote
