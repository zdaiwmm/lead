"""Durable, scoped local media jobs; no social account or paid service required."""

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from . import assets
from .config import ROOT, STATE, env
from .creative_schema import COVER_SIZES, PROFILES, RENDERER_VERSION, CreativeBrief
from .db import all_rows, audit, j, one, record, require_agent, tx, uid
from .resources import reserve_disk
from .scope import current_tenant

router = APIRouter()
RENDERERS = asyncio.Semaphore(2)
MAX_DAILY_JOBS = 30


def lock_path(aid, job_id):
    aid, job_id = str(UUID(str(aid))), str(UUID(str(job_id)))
    folder = STATE / "creative" / aid
    if folder.is_symlink() or folder.parent.is_symlink():
        raise ValueError("素材工作区路径异常")
    return folder / (job_id + ".lock")


def job_is_alive(aid, job_id):
    path = lock_path(aid, job_id)
    if not path.exists():
        return False
    if path.is_symlink():
        raise ValueError("素材任务锁状态异常")
    with path.open("a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def recover_interrupted(c, aid):
    for row in all_rows(
        c,
        "SELECT id FROM lead.tasks WHERE type='media.generate' AND state='running' AND started_at<now()-interval '4 minutes' FOR UPDATE SKIP LOCKED",
    ):
        if not job_is_alive(aid, row["id"]):
            c.execute(
                "UPDATE lead.tasks SET state='failed',result=%s,completed_at=now() WHERE id=%s",
                (
                    j({"error": "CREATIVE_PROCESS_INTERRUPTED", "retry_safe": True}),
                    row["id"],
                ),
            )


class CreativeRequest(BaseModel):
    platform: str = Field(pattern=r"^[a-z]+$", max_length=20)
    brief: CreativeBrief


def source_snapshot(business):
    return {
        "business_profile_id": str(business["id"]),
        "business_revision": business["revision"],
        "product_url": business.get("product_url"),
        "description_sha256": hashlib.sha256(
            business["description"].encode()
        ).hexdigest(),
        "source_refs": business.get("source_refs", []),
    }


def current_guard(c, aid, business_id, revision, control_version):
    agent = require_agent(c, aid, True)
    business = one(
        c, "SELECT id FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
    )
    if (
        agent["desired_state"] in {"paused", "archived"}
        or agent["config_revision"] != revision
        or agent["control_version"] != control_version
        or not business
        or str(business["id"]) != str(business_id)
    ):
        raise ValueError("素材制作期间运营状态或业务资料已变化")
    return agent


async def produce(
    aid,
    platform,
    brief,
    *,
    business_id=None,
    config_revision=None,
    purpose="publication",
):
    aid = str(UUID(str(aid)))
    from .admin_settings import brand_context, tool_enabled, validate_brand_content

    if not tool_enabled(aid, "media_creation"):
        raise ValueError("原创素材制作已在后台关闭")
    brief = CreativeBrief.model_validate(brief)
    with tx(aid) as c:
        validate_brand_content(c, aid, "\n".join([brief.headline, brief.cta, *[part for card in brief.cards for part in (card.heading, card.body)]]))
    profile_art = (
        purpose in {"profile_avatar", "profile_cover"} and brief.kind == "image"
    )
    if (
        platform not in PROFILES
        or (not profile_art and brief.kind not in PROFILES[platform])
        or (purpose == "profile_cover" and platform not in COVER_SIZES)
    ):
        raise ValueError("此渠道不支持该媒体制作格式")
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        if agent["desired_state"] == "paused":
            raise ValueError("Agent 已暂停，停止新的素材制作")
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if not business or not business["description"].strip():
            raise ValueError("需要已有业务介绍才能制作素材")
        if business_id and str(business["id"]) != str(business_id):
            raise ValueError("业务资料已经更新")
        if config_revision and agent["config_revision"] != config_revision:
            raise ValueError("运营配置已经更新")
        control = agent["control_version"]
        revision = agent["config_revision"]
        basis = source_snapshot(business)
        brand = brand_context(c, aid)
        basis["brand_configuration_version"] = brand["version"]
        payload = {
            "platform": platform,
            "brief": brief.model_dump(),
            "brand": brand["name"] or business.get("facts", {}).get("name") or agent["name"],
            "brand_configuration_version": brand["version"],
            "display_url": (business.get("product_url") or "")
            .replace("https://", "")
            .replace("http://", ""),
            "source_basis": basis,
            "renderer_version": RENDERER_VERSION,
            "config_revision": revision,
            "purpose": purpose,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        key = f"creative:{aid}:{digest}"
        old = one(c, "SELECT * FROM lead.tasks WHERE workflow_id=%s", (key,))
        if old and old["state"] == "succeeded":
            # Resolve every asset again; a corrupted/deleted asset is not a cache hit.
            for item in old["result"]["assets"]:
                assets.resolve(aid, item["id"], item["sha256"])
            return {**old["result"], "duplicate": True, "job_id": str(old["id"])}
        if (
            old
            and old["state"] == "running"
            and one(
                c,
                "SELECT 1 FROM lead.tasks WHERE id=%s AND started_at>now()-interval '4 minutes'",
                (old["id"],),
            )
        ):
            raise ValueError("同一份素材正在制作，请稍后继续")
        if old and old["state"] == "running" and job_is_alive(aid, old["id"]):
            raise ValueError("同一份素材正在制作，请稍后继续")
        if (
            one(
                c,
                "SELECT count(*) AS n FROM lead.tasks WHERE type='media.generate' AND created_at>now()-interval '1 day'",
            )["n"]
            >= MAX_DAILY_JOBS
            and not old
        ):
            raise ValueError("今天的素材制作量已达到平台上限")
        job_id = str(old["id"]) if old else uid()
        if old:
            c.execute(
                "UPDATE lead.tasks SET state='running',started_at=now(),completed_at=NULL,result=NULL WHERE id=%s",
                (job_id,),
            )
        else:
            c.execute(
                "INSERT INTO lead.tasks(id,tenant_id,agent_id,type,state,workflow_id,input,scheduled_at,started_at) VALUES(%s,%s,%s,'media.generate','running',%s,%s,now(),now())",
                (
                    job_id,
                    current_tenant(),
                    aid,
                    key,
                    j({**payload, "control_version": control}),
                ),
            )
    folder = STATE / "creative" / aid / job_id
    base = STATE / "creative"
    process = None
    held = None
    began = time.monotonic()
    try:
        for parent in (base, base / aid, folder):
            if parent.is_symlink():
                raise ValueError("素材工作区路径异常")
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        request = folder / "request.json"
        request.write_text(json.dumps(payload, ensure_ascii=False, default=str))
        request.chmod(0o600)
        lock = lock_path(aid, job_id)
        held = os.fdopen(
            os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a"
        )
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with RENDERERS:
            with reserve_disk(300 * 1024**2):
                process_env = {
                    "PATH": os.environ.get("PATH", ""),
                    "LANG": "en_US.UTF-8",
                    "PYTHONPATH": str(ROOT / "services/api"),
                }
                if env().get("LEAD_CREATIVE_FONT"):
                    process_env["LEAD_CREATIVE_FONT"] = env()["LEAD_CREATIVE_FONT"]
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "lead_app.creative_worker",
                    str(request),
                    cwd=ROOT,
                    env=process_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    pass_fds=(held.fileno(),),
                )
                output, _ = await asyncio.wait_for(process.communicate(), timeout=150)
                rendered = json.loads(output)
                if process.returncode:
                    raise ValueError(rendered.get("error", "CREATIVE_RENDER_FAILED"))
                with tx(aid) as c:
                    current_guard(c, aid, business["id"], revision, control)
                media = []
                filenames = [(rendered["filename"], "publication")]
                if rendered["preview"] != rendered["filename"]:
                    filenames.append((rendered["preview"], "preview"))
                for filename, role in filenames:
                    # Names are a fixed renderer contract, never model/user paths.
                    if filename not in {"poster.png", "explainer.mp4", "preview.png"}:
                        raise ValueError("制作结果格式异常")
                    file = folder / filename
                    item = await assets.upload(
                        aid,
                        UploadFile(file=file.open("rb"), filename=filename),
                        {
                            "source": "agent_original_editorial",
                            "ai_generated": True,
                            "job_id": job_id,
                            "source_basis": basis,
                            "presentation": "explainer",
                        },
                    )
                    with tx(aid) as c:
                        current_guard(c, aid, business["id"], revision, control)
                        annotation = {
                            "generated": {
                                "job_id": job_id,
                                "platform": platform,
                                "renderer_version": RENDERER_VERSION,
                                "source_basis": basis,
                                "brief": brief.model_dump(),
                                "role": role,
                                "purpose": purpose,
                                "config_revision": revision,
                                "quality": rendered["quality"],
                            }
                        }
                        c.execute(
                            "UPDATE lead.assets SET metadata=metadata || %s WHERE id=%s",
                            (j(annotation), item["id"]),
                        )
                    media.append(
                        {
                            "id": str(item["id"]),
                            "sha256": item["sha256"],
                            "role": role,
                            "kind": item["metadata"]["kind"],
                        }
                    )
                result = {
                    "assets": media,
                    "platform": platform,
                    "kind": brief.kind,
                    "captions": rendered["captions"],
                    "vtt": rendered["vtt"],
                    "quality": rendered["quality"],
                    "source_basis": basis,
                    "renderer_version": RENDERER_VERSION,
                    "brief": brief.model_dump(),
                    "render_elapsed_seconds": round(time.monotonic() - began, 3),
                }
                with tx(aid) as c:
                    current_guard(c, aid, business["id"], revision, control)
                    c.execute(
                        "UPDATE lead.tasks SET state='succeeded',result=%s,completed_at=now() WHERE id=%s",
                        (j(result), job_id),
                    )
                    audit(
                        c,
                        aid,
                        "creative.completed",
                        {"job_id": job_id, "platform": platform, "kind": brief.kind},
                    )
                    record(
                        c,
                        aid,
                        "已制作渠道素材",
                        platform + " · " + brief.headline,
                        task_id=job_id,
                    )
                return {**result, "job_id": job_id, "duplicate": False}
    except BaseException as error:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET state='failed',result=%s,completed_at=now() WHERE id=%s AND state='running'",
                (
                    j(
                        {
                            "error": str(error)[:240]
                            if isinstance(error, ValueError)
                            else type(error).__name__,
                            "retry_safe": True,
                        }
                    ),
                    job_id,
                ),
            )
        raise
    finally:
        if held:
            held.close()
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)


@router.post("/api/agents/{aid}/creative")
async def create_creative(aid: UUID, request: CreativeRequest):
    return await produce(str(aid), request.platform, request.brief)


@router.get("/api/agents/{aid}/creative")
def creative_jobs(aid: UUID):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        return all_rows(
            c,
            "SELECT id,state,input->>'platform' AS platform,input->'brief' AS brief,result,created_at,completed_at FROM lead.tasks WHERE type='media.generate' ORDER BY created_at DESC LIMIT 40",
        )


@router.get("/api/agents/{aid}/creative/{job_id}/captions.vtt")
def captions(aid: UUID, job_id: UUID):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        job = one(
            c,
            "SELECT result FROM lead.tasks WHERE id=%s AND type='media.generate' AND state='succeeded'",
            (job_id,),
        )
        if not job:
            raise LookupError("字幕不存在")
    return Response(
        job["result"]["vtt"],
        media_type="text/vtt",
        headers={"Content-Disposition": 'attachment; filename="captions.vtt"'},
    )
