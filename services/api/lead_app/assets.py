"""Owner-scoped media storage. Browser tools accept asset IDs, never file paths."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from .config import ROOT, STATE
from .db import all_rows, audit, j, one, require_agent, tx, uid
from .scope import current_tenant

MAX_UPLOAD = 128 * 1024**2
DECODERS = asyncio.Semaphore(2)


async def upload(aid, incoming, rights=None):
    from .resources import reserve_disk

    with reserve_disk(2 * MAX_UPLOAD):
        return await _upload(aid, incoming, rights)


async def _upload(aid, incoming, rights=None):
    from .resources import ensure_capacity

    ensure_capacity(256)
    aid = str(UUID(aid))
    asset_id = uid()
    with tx(aid) as c:
        require_agent(c, aid)
    folder = STATE / "assets" / aid
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink() or folder.resolve().parent != (STATE / "assets").resolve():
        raise ValueError("素材存储状态异常")
    temp = folder / (asset_id + ".upload")
    base = folder / asset_id
    total = 0
    try:
        with temp.open("xb") as out:
            os.chmod(temp, 0o600)
            while chunk := await incoming.read(1024**2):
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise ValueError("文件不能超过 128 MB")
                out.write(chunk)
        if not total:
            raise ValueError("文件为空")
        await DECODERS.acquire()
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "lead_app.media_worker",
                str(temp),
                str(base),
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "services/api")},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except BaseException:
            DECODERS.release()
            raise
        try:
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=45)
            except BaseException:
                process.kill()
                await process.wait()
                raise ValueError("媒体检查超时，尚未保存") from None
        finally:
            DECODERS.release()
        if process.returncode:
            raise ValueError("文件不是可用的图片或视频，或超出当前媒体限制")
        info = json.loads(stdout)
        path = base.with_suffix(info["extension"])
        path.chmod(0o600)
        if not 0 < path.stat().st_size <= MAX_UPLOAD:
            raise ValueError("处理后的媒体过大")
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        with tx(aid) as c:
            require_agent(c, aid, True)
            used = one(
                c,
                "SELECT coalesce(sum(size_bytes),0) AS n FROM lead.assets WHERE state='ready'",
            )["n"]
            if used + path.stat().st_size > 1024**3:
                raise ValueError("此 Agent 的素材空间已满")
            existing = one(
                c,
                "SELECT * FROM lead.assets WHERE sha256=%s AND state='ready'",
                (digest,),
            )
            if existing:
                path.unlink(missing_ok=True)
                return public(existing)
            row = one(
                c,
                "INSERT INTO lead.assets(id,tenant_id,agent_id,object_ref,media_type,size_bytes,sha256,rights,state,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'ready',%s) RETURNING *",
                (
                    asset_id,
                    current_tenant(),
                    aid,
                    str(path),
                    info["media_type"],
                    path.stat().st_size,
                    digest,
                    j(
                        rights
                        or {
                            "source": "authenticated_owner_upload",
                            "ai_generated": None,
                        }
                    ),
                    j(info),
                ),
            )
            audit(
                c,
                aid,
                "asset.uploaded",
                {"asset_id": asset_id, "sha256": digest, "kind": info["kind"]},
            )
        return public(row)
    finally:
        await incoming.close()
        temp.unlink(missing_ok=True)
        # Clean files that never reached the committed, scoped catalogue.
        with tx(aid) as c:
            record = one(c, "SELECT id FROM lead.assets WHERE id=%s", (asset_id,))
        if not record:
            for path in folder.glob(asset_id + ".*"):
                path.unlink(missing_ok=True)


def public(row):
    return {
        key: row[key]
        for key in (
            "id",
            "media_type",
            "size_bytes",
            "sha256",
            "metadata",
            "state",
            "created_at",
        )
    }


def listing(aid):
    with tx(aid) as c:
        require_agent(c, aid)
        return [
            public(a)
            for a in all_rows(
                c,
                "SELECT * FROM lead.assets WHERE state='ready' ORDER BY created_at DESC",
            )
        ]


def resolve(aid, asset_id, expected_sha=None):
    with tx(aid) as c:
        row = one(
            c,
            "SELECT * FROM lead.assets WHERE id=%s AND state='ready'",
            (str(UUID(str(asset_id))),),
        )
    if not row:
        raise LookupError("素材不存在")
    folder = STATE / "assets" / str(UUID(aid))
    root = folder.resolve()
    path = Path(row["object_ref"])
    if (
        folder.is_symlink()
        or root.parent != (STATE / "assets").resolve()
        or path.is_symlink()
        or path.resolve().parent != root
        or not path.is_file()
    ):
        raise ValueError("素材存储状态异常")
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    if digest != row["sha256"] or expected_sha and expected_sha != digest:
        raise ValueError("素材内容已变化，禁止上传")
    return row, path
