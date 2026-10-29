"""Encrypted immutable-material snapshots paired with each database snapshot."""

import hashlib
import io
import json
import os
import shutil
import tarfile
from contextlib import nullcontext
from pathlib import Path, PurePosixPath

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .backup_crypto import EncryptedWriter, decrypt_to_file
from .config import STATE, database_kwargs
from .resources import reserve_disk

REFERENCES = (
    ("assets", "object_ref"),
    ("knowledge_documents", "body_ref"),
    ("browser_steps", "screenshot_ref"),
    ("actions", "evidence_ref"),
    ("action_attempts", "evidence_ref"),
    ("identity_checks", "evidence_ref"),
)


class HashingReader:
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()

    def read(self, size=-1):
        value = self.stream.read(size)
        self.hash.update(value)
        return value


def referenced_files(connection=None):
    files = {}
    missing_archived = 0
    with (
        nullcontext(connection)
        if connection is not None
        else psycopg.connect(**database_kwargs(True), row_factory=dict_row)
    ) as c:
        for table, column in REFERENCES:
            rows = c.execute(
                sql.SQL(
                    "SELECT x.{col} AS reference,a.desired_state FROM lead.{table} x JOIN lead.agents a ON a.id=x.agent_id WHERE x.{col} IS NOT NULL"
                ).format(col=sql.Identifier(column), table=sql.Identifier(table))
            ).fetchall()
            for row in rows:
                path = Path(row["reference"])
                try:
                    relative = path.relative_to(STATE)
                except ValueError:
                    continue  # Remote object references are not host file paths.
                if not relative.parts or relative.parts[0] not in (
                    "assets",
                    "evidence",
                ):
                    continue
                if (
                    ".." in relative.parts
                    or path.is_symlink()
                    or not path.resolve().is_relative_to(STATE.resolve())
                ):
                    raise ValueError("资料文件路径不可用于备份")
                if not path.is_file():
                    if row["desired_state"] == "archived":
                        missing_archived += 1
                        continue
                    raise ValueError("活跃业务的资料文件缺失，备份未通过")
                files[relative.as_posix()] = path
    return files, missing_archived


def capture(folder, key, identifier, managed=False, references=None):
    from .backups import catalog, destination, digest, now

    files, missing = references if references is not None else referenced_files()
    metadata = {
        "id": identifier,
        "kind": "data_files",
        "started_at": now(),
        "managed": managed,
    }
    manifest = {
        "source_root": str(STATE),
        "missing_archived_references": missing,
        "files": {
            name: {"size": path.stat().st_size, "sha256": digest(path)}
            for name, path in files.items()
        },
    }
    path = destination(folder, identifier)
    with reserve_disk(
        sum(item["size"] for item in manifest["files"].values()) + 64 * 1024**2
    ):
        writer = EncryptedWriter(path, key, metadata)
        try:
            with tarfile.open(fileobj=writer, mode="w|") as archive:
                data = json.dumps(manifest, sort_keys=True).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(data))
                for name, source in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size = manifest["files"][name]["size"]
                    info.mode = 0o600
                    with source.open("rb") as stream:
                        hashed = HashingReader(stream)
                        archive.addfile(info, hashed)
                    if hashed.hash.hexdigest() != manifest["files"][name]["sha256"]:
                        raise ValueError("资料文件在备份中发生变化，未保存恢复点")
            writer.finish()
        except BaseException:
            writer.abort()
            raise
    return catalog(
        path,
        {**metadata, "files_count": len(files), "missing_archived_references": missing},
        key,
    )


def restore(source, key, identifier, folder):
    from .backups import digest

    plain = folder / "data-files.tar"
    metadata = decrypt_to_file(source, plain, key, maximum=8 * 1024**3)
    if metadata.get("id") != identifier or metadata.get("kind") != "data_files":
        raise ValueError("资料快照与数据库备份不一致")
    staging = folder / "data-files"
    staging.mkdir(mode=0o700)
    total = 0
    count = 0
    with tarfile.open(plain, "r:") as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not member.isfile()
                or not relative.parts
                or relative.parts[0] not in ("manifest.json", "assets", "evidence")
            ):
                raise ValueError("资料归档路径不可用")
            total += member.size
            count += 1
            if total > 8 * 1024**3 or count > 200000:
                raise ValueError("资料归档超过当前恢复限制")
            target = staging.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as stream:
                os.chmod(target, 0o600)
                shutil.copyfileobj(archive.extractfile(member), stream, 1024**2)
    manifest = json.loads((staging / "manifest.json").read_text())
    actual = {
        path.relative_to(staging).as_posix(): path
        for path in staging.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if set(actual) != set(manifest["files"]):
        raise ValueError("资料文件清单不一致")
    for name, path in actual.items():
        expected = manifest["files"][name]
        if (
            path.stat().st_size != expected["size"]
            or digest(path) != expected["sha256"]
        ):
            raise ValueError("资料文件校验未通过")
    return manifest, actual


def install(manifest, files, connection):
    from .backups import digest

    installed = 0
    for name, path in files.items():
        target = STATE.joinpath(*PurePosixPath(name).parts)
        if target.is_symlink() or not target.resolve().is_relative_to(STATE.resolve()):
            raise ValueError("恢复目标路径不可用")
        if target.exists():
            if (
                not target.is_file()
                or digest(target) != manifest["files"][name]["sha256"]
            ):
                raise ValueError("现有资料与恢复文件不同，拒绝覆盖")
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as output, path.open("rb") as source:
            os.chmod(target, 0o600)
            shutil.copyfileobj(source, output, 1024**2)
            output.flush()
            os.fsync(output.fileno())
        installed += 1
    original = Path(manifest["source_root"])
    if original != STATE:
        for table, column in REFERENCES:
            for identifier, reference in connection.execute(
                sql.SQL(
                    "SELECT id,{column} FROM lead.{table} WHERE {column} IS NOT NULL"
                ).format(column=sql.Identifier(column), table=sql.Identifier(table))
            ).fetchall():
                try:
                    relative = Path(reference).relative_to(original)
                except ValueError:
                    continue
                if relative.as_posix() not in files:
                    continue
                connection.execute(
                    sql.SQL("UPDATE lead.{table} SET {column}=%s WHERE id=%s").format(
                        table=sql.Identifier(table), column=sql.Identifier(column)
                    ),
                    (str(STATE / relative), identifier),
                )
    return installed
