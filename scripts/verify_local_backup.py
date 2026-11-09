"""Create a local encrypted recovery point and restore it on a private, temporary PostgreSQL cluster."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app import backup_files, backups
from lead_app.backup_crypto import decrypt_to_file, load_key
from lead_app.config import database_kwargs


def drill(record, folder, key, work, pg, pg_share):
    backup_id = str(UUID(record["id"]))
    files_id = str(UUID(record["files_snapshot_id"]))
    database = folder / (backup_id + ".leadbackup")
    files = folder / (files_id + ".leadbackup")
    if (
        backups.digest(database) != record["cipher_sha256"]
        or backups.digest(files) != record["files_snapshot_sha256"]
    ):
        raise ValueError("加密归档与备份台账不符")
    with (
        tempfile.TemporaryDirectory(dir=work) as staging,
        tempfile.TemporaryDirectory(dir="/tmp", prefix="lead-drill-") as sock,
    ):
        staging = Path(staging)
        dump = staging / "database.dump"
        header = decrypt_to_file(database, dump, key)
        if (
            header.get("id") != backup_id
            or header.get("kind") != "database"
            or header.get("files_snapshot_id") != files_id
        ):
            raise ValueError("加密备份标识不一致")
        manifest, restored = backup_files.restore(files, key, files_id, staging)
        if len(restored) != len(manifest["files"]):
            raise ValueError("关联资料回读不完整")
        cluster = staging / "pgdata"
        environ = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "HOME": str(staging),
            "PGHOST": sock,
            "PGPORT": "27472",
            "PGUSER": "lead_owner",
            "PGDATABASE": "postgres",
            "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
        }

        def tool(name, *args, **options):
            return subprocess.run(
                [str(pg / name), *map(str, args)],
                check=True,
                env=environ,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=300,
                **options,
            )

        tool(
            "initdb",
            "-D",
            cluster,
            "-U",
            "lead_owner",
            "-A",
            "trust",
            "--locale=C",
            "--encoding=UTF8",
            "-L",
            pg_share,
        )
        tool(
            "pg_ctl",
            "-D",
            cluster,
            "-l",
            staging / "postgres.log",
            "-o",
            f"-k {sock} -p 27472 -c listen_addresses='' -c unix_socket_permissions=0700 -c max_connections=20",
            "-w",
            "start",
        )
        try:
            with psycopg.connect(
                host=sock,
                port=27472,
                user="lead_owner",
                dbname="postgres",
                autocommit=True,
                row_factory=dict_row,
            ) as isolated:
                for role in (
                    "lead_runtime",
                    "lead_auth",
                    "lead_identity_owner",
                    "lead_analytics_maintenance",
                ):
                    isolated.execute(
                        sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role))
                    )
                with dump.open("rb") as stream:
                    tool(
                        "pg_restore",
                        "--dbname",
                        "postgres",
                        "--no-owner",
                        "--no-privileges",
                        "--single-transaction",
                        "--exit-on-error",
                        stdin=stream,
                    )
                counts = backups.verification_counts(isolated)
                if counts != header.get("verification_counts") or counts != record.get(
                    "verification_counts"
                ):
                    raise ValueError("恢复库与导出快照的业务计数不一致")
        finally:
            tool("pg_ctl", "-D", cluster, "-m", "immediate", "-w", "stop")
    return {
        "kind": "isolated_postgres",
        "checked_at": backups.now(),
        "cipher_sha256": record["cipher_sha256"],
        "files_sha256": record["files_snapshot_sha256"],
        "restored_counts": counts,
        "files_checked": len(restored),
    }


def main():
    parser = argparse.ArgumentParser()
    for arg in ("folder", "key", "work", "pg", "pg-share"):
        parser.add_argument("--" + arg, required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() == 0 or backups.backup_policy() != "verified_local":
        raise ValueError("需要非 root 服务身份及本地恢复验证策略")
    key = load_key(args.key)
    args.work.mkdir(mode=0o700, parents=True, exist_ok=True)
    with backups.backup_lock(args.folder):
        record = backups._backup_database(args.folder, key, True)
        try:
            proof = drill(record, args.folder, key, args.work, args.pg, args.pg_share)
        except Exception:
            # A failed drill may run again on the timer. Keep the last good pair and
            # discard only the two new, unusable files created under this lock.
            identifiers = [UUID(record["id"]), UUID(record["files_snapshot_id"])]
            with psycopg.connect(**database_kwargs(True)) as c:
                c.execute(
                    "UPDATE lead.backup_records SET state='unavailable' WHERE id=ANY(%s)",
                    (identifiers,),
                )
            for identifier in identifiers:
                (args.folder / (str(identifier) + ".leadbackup")).unlink(
                    missing_ok=True
                )
            raise
        with psycopg.connect(**database_kwargs(True)) as c:
            updated = c.execute(
                "UPDATE lead.backup_records SET manifest=jsonb_set(manifest,'{local_restore}',%s) WHERE id=%s AND state='verified' AND cipher_sha256=%s RETURNING id",
                (Jsonb(proof), record["id"], record["cipher_sha256"]),
            ).fetchone()
            if (
                not updated
                or not c.execute("SELECT lead.local_restore_ready()").fetchone()[0]
            ):
                raise ValueError("本次本地恢复未在有效时间窗内取得凭证")
        backups._prune_managed(args.folder, 4 * 1024**3)
    print(
        json.dumps(
            {
                "backup_id": record["id"],
                "verified_local": True,
                "offsite": False,
                **proof,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
