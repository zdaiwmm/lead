import asyncio
import hashlib
import html
import re
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, Field

from . import domain
from .admin_advanced_api import router as advanced_admin_router
from .admin_api import router as admin_router
from .analytics import router as analytics_router
from .auth import auth_mode, public_origin
from .auth import router as auth_router
from .community_growth import GrowthConfig
from .config import STATE, env, model_config
from .conversions import router as conversion_router
from .creative_pipeline import router as creative_router
from .db import all_rows, audit, one, record, require_agent, tx
from .events import router as event_router
from .operating_playbooks import router as playbook_router
from .operating_workspace import router as operations_router
from .ops import router as ops_router
from .scope import current_tenant, tenant_scope
from .security import internal_token, token, valid
from .telemetry import TelemetryMiddleware, trace_headers
from .upload_limit import UploadLimit

app = FastAPI(title="lead", version="0.1.0", docs_url=None, redoc_url=None)
app.add_middleware(UploadLimit)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[
        "127.0.0.1",
        "localhost",
        *([] if env().get("LEAD_DEPLOYMENT_MODE") == "production" else ["testserver"]),
        urlsplit(public_origin()).hostname,
    ],
)
ORIGINS = {"http://localhost:3100", "http://127.0.0.1:3100", "http://127.0.0.1:8088"}
app.include_router(analytics_router)

app.include_router(event_router)
app.include_router(conversion_router)
app.include_router(auth_router)
app.include_router(ops_router)
app.include_router(playbook_router)
app.include_router(creative_router)
app.include_router(operations_router)
app.include_router(admin_router)
app.include_router(advanced_admin_router)


@app.middleware("http")
async def local_auth(request, call_next):
    path = request.url.path
    identity = None
    public_workspace = None
    if env().get("LEAD_DEPLOYMENT_MODE") == "production" and auth_mode() != "oidc":
        return JSONResponse({"detail": "生产服务需要启用工作室登录"}, status_code=503)
    if path.startswith("/api/"):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if (
                request.headers.get("origin")
                not in (
                    {public_origin()}
                    if env().get("LEAD_DEPLOYMENT_MODE") == "production"
                    else ORIGINS | {public_origin()}
                )
                or request.headers.get("x-lead-request") != "1"
            ):
                return JSONResponse({"detail": "请求来源不可用"}, status_code=403)
        public_auth = path in ("/api/session", "/api/auth/login", "/api/auth/callback")
        if not public_auth:
            if auth_mode() == "oidc":
                from .identity_store import lookup_session

                identity = await asyncio.to_thread(
                    lookup_session, request.cookies.get("lead_session", "")
                )
            elif valid(request.cookies.get("lead_session", ""), "owner"):
                identity = {
                    "tenant_id": current_tenant(),
                    "role": "owner",
                    "subject": "local",
                    "issuer": "local",
                }
            if not identity:
                return JSONResponse(
                    {"detail": "请登录工作室", "login_url": "/api/auth/login"},
                    status_code=401,
                )
            expected = request.headers.get("x-lead-workspace")
            if expected and expected != identity["tenant_id"]:
                return JSONResponse(
                    {
                        "detail": "工作室已切换，请重新打开页面",
                        "code": "workspace_changed",
                    },
                    status_code=409,
                )
            sensitive = path.endswith("/browser/screenshot")
            viewer_exception = path in ("/api/auth/logout", "/api/workspace/switch")
            viewer_exception = viewer_exception or bool(
                re.fullmatch(r"/api/admin/notifications/[0-9a-fA-F-]{36}/read", path)
            )
            if identity["role"] == "viewer" and (
                (
                    request.method not in ("GET", "HEAD", "OPTIONS")
                    and not viewer_exception
                )
                or sensitive
            ):
                return JSONResponse({"detail": "当前成员仅有查看权限"}, status_code=403)
            from .admin_permissions import require as require_capability
            from .admin_permissions import resolve, workbench_permission

            with tenant_scope(identity["tenant_id"], identity):
                identity.update(await asyncio.to_thread(resolve, identity))
                agent_path = re.match(r"^/api/agents/([0-9a-fA-F-]{36})(?:/|$)", path)
                requested_agent = (
                    agent_path.group(1)
                    if agent_path
                    else request.query_params.get("aid")
                )
                if identity.get("agent_ids") is not None and requested_agent:
                    try:
                        scoped_agent = str(UUID(requested_agent))
                    except ValueError:
                        return JSONResponse(
                            {"detail": "Agent 标识无效"}, status_code=422
                        )
                    if scoped_agent not in identity["agent_ids"]:
                        return JSONResponse({"detail": "Agent 不存在"}, status_code=404)
                capability = workbench_permission(request.method, path)
                if capability:
                    try:
                        require_capability(
                            capability, workspace=capability == "agents.create"
                        )
                    except HTTPException as error:
                        return JSONResponse(
                            {"detail": error.detail}, status_code=error.status_code
                        )
    elif re.fullmatch(r"/r/[A-Za-z0-9_-]{16}", path) or re.fullmatch(
        r"/collect/[0-9a-fA-F-]{36}", path
    ):
        from .identity_store import public_tenant

        public_workspace = await asyncio.to_thread(
            public_tenant,
            "link" if path.startswith("/r/") else "analytics",
            path.rsplit("/", 1)[-1],
        )
    elif path.startswith("/lab/"):
        aid = path.split("/")[2]
        if valid(request.cookies.get("lead_lab", ""), "lab:" + aid):
            from .identity_store import agent_tenant

            public_workspace = await asyncio.to_thread(agent_tenant, aid)
    with ExitStack() as stack:
        if identity:
            stack.enter_context(tenant_scope(identity["tenant_id"], identity))
        elif public_workspace:
            stack.enter_context(tenant_scope(public_workspace))
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if identity:
        response.headers["X-Lead-Workspace"] = identity["tenant_id"]
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = (
        "no-store, no-transform" if path == "/api/events" else "no-store"
    )
    if path.startswith("/lab/"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
        )
        if path.endswith("/identity/"):
            from .identity import SCRIPT_HASH

            response.headers["Content-Security-Policy"] = (
                f"default-src 'none'; script-src 'sha256-{SCRIPT_HASH}'; frame-ancestors 'none'"
            )
    return response


@app.exception_handler(ValueError)
async def value_error(request, e):
    return JSONResponse({"detail": str(e)}, status_code=409)


@app.exception_handler(LookupError)
async def lookup_error(request, e):
    return JSONResponse({"detail": str(e)}, status_code=404)


class CreateAgent(BaseModel):
    client_id: UUID | None = None


class AgentSetupProfile(BaseModel):
    product_name: str = Field(min_length=1, max_length=80)
    product_url: str = Field(default="", max_length=2000)
    description: str = Field(min_length=1, max_length=10000)
    audience: str = Field(default="", max_length=2000)
    goal: str = Field(min_length=1, max_length=2000)
    content_expectations: str = Field(default="", max_length=2000)
    strategy: str = Field(min_length=1, max_length=2000)
    platform: str

    def profile(self):
        data = {
            k: v.strip()
            for k, v in self.model_dump().items()
            if k in AgentSetupProfile.model_fields
        }
        if (
            not data["product_name"]
            or not data["description"]
            or not data["goal"]
            or not data["strategy"]
        ):
            raise ValueError("请补充产品信息和运营方向")
        if data["product_url"] and (
            urlsplit(data["product_url"]).scheme not in ("http", "https")
            or not urlsplit(data["product_url"]).hostname
        ):
            raise ValueError("请输入完整的网站地址")
        return data


class AgentSetupCreate(AgentSetupProfile):
    client_id: UUID
    template_version: int | None = Field(default=None, ge=0)


class AgentSetupEdit(AgentSetupProfile):
    expected_revision: int = Field(ge=1)


@app.post("/api/agents/setup", status_code=201)
def create_setup(body: AgentSetupCreate):
    aid = domain.create_agent(
        str(body.client_id),
        setup=body.profile(),
        setup_template_version=body.template_version,
    )
    a = domain.agent_detail(aid)
    return {"id": aid, "config_revision": a["config_revision"]}


@app.post("/api/agents/{aid}/setup")
def edit_setup(aid: UUID, body: AgentSetupEdit):
    return domain.update_setup(str(aid), body.profile(), body.expected_revision)


@app.post("/api/agents/{aid}/setup/complete")
def complete_setup(aid: UUID):
    return domain.complete_setup(str(aid))


@app.post("/api/agents/{aid}/setup/cancel")
async def cancel_setup(aid: UUID):
    changed = await asyncio.to_thread(
        domain.set_control, str(aid), "archive", only_setup=True
    )
    if changed:
        await gateway(f"/agents/{aid}/close", body={})
    return {"ok": True}


@app.get("/api/agents/{aid}/activity")
def activity(aid: UUID, platform: str | None = None, limit: int = 50, offset: int = 0):
    return domain.agent_activity(str(aid), platform, limit, offset)


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    client_message_id: UUID
    asset_ids: list[UUID] = Field(default_factory=list, max_length=10)


@app.post("/api/agents/{aid}/assets")
async def upload_asset(aid: UUID, file: UploadFile = File(...)):
    from .assets import upload

    return await upload(str(aid), file)


@app.get("/api/agents/{aid}/assets")
def list_assets(aid: UUID):
    from .assets import listing

    return listing(str(aid))


@app.get("/api/agents/{aid}/usage")
def agent_usage(aid: UUID):
    from .metering import summary

    return summary(str(aid))


class TokenBudget(BaseModel):
    daily_token_limit: int | None = Field(strict=True, ge=0, le=9_007_199_254_740_991)
    expected_version: int = Field(ge=0)


@app.get("/api/agents/{aid}/budget")
def agent_budget(aid: UUID):
    from .budget import get_configuration

    return get_configuration(str(aid))


@app.post("/api/agents/{aid}/budget")
def set_agent_budget(aid: UUID, body: TokenBudget):
    from .admin_permissions import require
    from .budget import set_daily_limit

    require("admin.budgets")
    return set_daily_limit(str(aid), body.daily_token_limit, body.expected_version)


@app.get("/api/agents/{aid}/export")
def export_agent(aid: UUID):
    from .lifecycle import export_archive

    return Response(
        export_archive(str(aid)),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="lead-{aid}.zip"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/agents/{aid}/assets/{asset_id}")
def get_asset(aid: UUID, asset_id: UUID):
    from .assets import resolve

    asset, path = resolve(str(aid), str(asset_id))
    return FileResponse(
        path,
        media_type=asset["media_type"],
        filename=path.name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'",
        },
    )


class Control(BaseModel):
    action: Literal["pause", "resume", "start", "archive"]
    platform: str | None = None


class ChannelControl(BaseModel):
    enabled: bool


class AgentIdentity(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    avatar_seed: int = Field(ge=0, le=15)
    avatar_asset_id: UUID | None = None


class BusinessEdit(BaseModel):
    expected_revision: int = Field(ge=1)
    product_url: str = Field(default="", max_length=2000)
    description: str = Field(max_length=10000)
    audience: str = Field(default="", max_length=2000)


class PreferencesEdit(BaseModel):
    expected_revision: int = Field(ge=1)
    entries: list[str] = Field(max_length=100)


@app.post("/api/agents/{aid}/identity")
def edit_identity(aid: UUID, body: AgentIdentity):
    if not body.name.strip():
        raise ValueError("名称不能为空")
    return domain.update_identity(
        str(aid),
        body.name.strip(),
        body.avatar_seed,
        str(body.avatar_asset_id) if body.avatar_asset_id else None,
    )


@app.post("/api/agents/{aid}/business")
def edit_business(aid: UUID, body: BusinessEdit):
    if body.product_url and (
        urlsplit(body.product_url).scheme not in ("http", "https")
        or not urlsplit(body.product_url).hostname
    ):
        raise ValueError("请输入完整的网站地址")
    return domain.update_business(
        str(aid),
        body.expected_revision,
        body.product_url,
        body.description.strip(),
        body.audience.strip(),
    )


@app.post("/api/agents/{aid}/preferences")
def edit_preferences(aid: UUID, body: PreferencesEdit):
    if any(len(text) > 4000 for text in body.entries):
        raise ValueError("每条偏好最多 4000 字")
    return domain.update_preferences(str(aid), body.expected_revision, body.entries)


@app.post("/api/agents/{aid}/channels/{platform}/control")
def edit_channel_control(aid: UUID, platform: str, body: ChannelControl):
    return domain.channel_control(str(aid), platform, body.enabled)


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


@app.post("/api/agents/{aid}/memory/{memory_id}/remove")
def remove_preference(aid: UUID, memory_id: UUID, body: RevisionRequest):
    return domain.forget_preference(str(aid), str(memory_id), body.expected_revision)


class Connection(BaseModel):
    platform: str


class LabPublish(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    key: str = Field(pattern=r"^[A-Za-z0-9:_.-]{1,100}$")


class BrowserInput(BaseModel):
    kind: Literal["click", "text", "key", "scroll", "back", "forward", "reload"]
    x: float = 0
    y: float = 0
    text: str = Field(default="", max_length=4000)


class CardResponse(BaseModel):
    version: int = Field(ge=1)
    accepted: bool


class AccountTargetRequest(BaseModel):
    url: str = Field(max_length=2000)


@app.post("/api/agents/{aid}/telegram/verify-login")
async def verify_telegram(aid: UUID):
    return await verify_login(aid, Connection(platform="telegram"))


@app.post("/api/agents/{aid}/accounts/verify-login")
async def verify_login(aid: UUID, body: Connection):
    from .adapters.catalog import SPECS
    from .channels import store_account, store_target

    platform = body.platform
    if platform not in SPECS:
        raise ValueError("频道不存在")
    aid = str(aid)
    observed = (
        await gateway(f"/agents/{aid}/account/inspect", body={"platform": platform})
    ).json()
    auto = {
        "instagram": f"https://www.instagram.com/{observed['external_account_id']}/",
        "x": f"https://x.com/{observed['external_account_id']}/",
        "threads": f"https://www.threads.com/@{observed['external_account_id']}/",
        "tiktok": f"https://www.tiktok.com/@{observed['external_account_id']}/",
        "linkedin": f"https://www.linkedin.com/in/{observed['external_account_id']}/",
        "youtube": f"https://www.youtube.com/channel/{observed['external_account_id']}/",
    }
    account_id = await asyncio.to_thread(
        store_account, aid, platform, observed, platform not in auto
    )
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state IN ('active','requested','ending')"
        )
    if platform in auto:
        target = (
            await gateway(
                f"/agents/{aid}/target/inspect",
                body={"account_id": account_id, "url": auto[platform]},
            )
        ).json()
        await asyncio.to_thread(store_target, aid, account_id, target)
    return {"ok": True, "account_id": account_id}


@app.post("/api/agents/{aid}/identity/check")
async def check_identity(aid: UUID):
    return (await gateway(f"/agents/{aid}/identity/check")).json()


@app.post("/api/agents/{aid}/accounts/{account_id}/targets")
async def bind_target(aid: UUID, account_id: UUID, body: AccountTargetRequest):
    from .channels import store_target

    observed = (
        await gateway(
            f"/agents/{aid}/target/inspect",
            body={"account_id": str(account_id), "url": body.url},
        )
    ).json()
    return await asyncio.to_thread(store_target, str(aid), str(account_id), observed)


class VerificationRequest(BaseModel):
    asset_ids: list[UUID] = Field(default_factory=list, max_length=10)
    ai_generated: bool | None = None
    made_for_kids: bool | None = None


@app.post("/api/agents/{aid}/targets/{target_id}/verification")
def create_verification(aid: UUID, target_id: UUID, body: VerificationRequest):
    from .channels import create_verification

    return create_verification(
        str(aid),
        str(target_id),
        [str(x) for x in body.asset_ids],
        body.made_for_kids,
        body.ai_generated,
    )


@app.post("/api/agents/{aid}/cards/{card_id}/respond")
def respond_card(aid: UUID, card_id: UUID, body: CardResponse):
    from .channels import accept_card

    return accept_card(str(aid), str(card_id), body.version, body.accepted)


async def gateway(path, method="POST", body=None, timeout=100):
    matched = re.match(r"^/agents/([0-9a-fA-F-]{36})(?:/|$)", path)
    if matched:
        with tx(matched[1]) as c:
            if path == "/agents/" + matched[1] + "/close":
                if not one(c, "SELECT id FROM lead.agents WHERE id=%s", (matched[1],)):
                    raise LookupError("Agent 不存在")
            else:
                require_agent(c, matched[1])
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        try:
            r = await client.request(
                method,
                "http://127.0.0.1:8089" + path,
                json=body,
                headers={
                    "Authorization": "Bearer " + internal_token(),
                    **trace_headers(),
                },
            )
        except httpx.HTTPError:
            raise HTTPException(503, "浏览器服务暂不可用，请稍后重试") from None
        if r.status_code >= 400:
            if r.status_code == 409:
                detail = r.json().get("detail", "浏览器状态不允许此操作")
                raise HTTPException(409, detail)
            raise HTTPException(409, "浏览器操作未完成，请重试或查看运行记录")
        return r


@app.post("/api/session")
def session(request: Request):
    if auth_mode() == "oidc":
        from .identity_store import lookup_session

        who = lookup_session(request.cookies.get("lead_session", ""))
        return {
            "ok": bool(who),
            "role": who["role"] if who else None,
            "login_url": None if who else "/api/auth/login",
        }
    r = JSONResponse({"ok": True})
    r.set_cookie(
        "lead_session", token("owner"), httponly=True, samesite="strict", max_age=86400
    )
    return r


@app.get("/api/health")
async def health():
    db_ok = False
    try:
        with tx() as c:
            db_ok = bool(one(c, "SELECT 1 AS ok"))
    except Exception:
        pass
    worker_file = STATE / "worker-heartbeat"
    worker_ok = worker_file.exists() and time.time() - worker_file.stat().st_mtime < 20
    browser_ok = False
    active_profiles = 0
    try:
        data = (await gateway("/health", "GET", timeout=2)).json()
        browser_ok = data["ok"]
        active_profiles = data["active_profiles"]
    except Exception:
        pass
    return {
        "database": db_ok,
        "worker": worker_ok,
        "browser": browser_ok,
        "active_profiles": active_profiles,
        "model_configured": bool(model_config()["key"]),
        "model": model_config()["model"],
        "browser_capacity": int(env().get("LEAD_BROWSER_CONCURRENCY") or 3),
        "mode": auth_mode(),
    }


@app.get("/api/agents")
def agents():
    return domain.list_agents()


@app.post("/api/agents", status_code=201)
def create(body: CreateAgent):
    aid = domain.create_agent(str(body.client_id) if body.client_id else None)
    return {"id": aid}


@app.get("/api/agents/{aid}")
def detail(aid: UUID):
    return domain.agent_detail(str(aid))


@app.get("/api/agents/{aid}/browser-runs/{rid}")
def browser_run(aid: UUID, rid: UUID):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        run = one(c, "SELECT * FROM lead.browser_runs WHERE id=%s", (rid,))
        if not run:
            raise HTTPException(404, "浏览记录不存在")
        steps = all_rows(
            c,
            "SELECT id,seq,operation,input,state,output,error_code,started_at,ended_at FROM lead.browser_steps WHERE run_id=%s ORDER BY seq",
            (rid,),
        )
        for s in steps:
            if s["output"]:
                s["page"] = {
                    k: s["output"].get(k)
                    for k in ("url", "title", "text", "evidence_id")
                }
                del s["output"]
            s["screenshot_url"] = (
                f"/api/agents/{aid}/browser-evidence/{s['id']}"
                if s["state"] == "completed"
                else None
            )
        return {**run, "steps": steps}


@app.get("/api/agents/{aid}/browser-evidence/{evidence_id}")
def browser_evidence(aid: UUID, evidence_id: UUID):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        step = one(
            c,
            "SELECT screenshot_ref FROM lead.browser_steps WHERE id=%s",
            (evidence_id,),
        )
        if not step or not step["screenshot_ref"]:
            raise HTTPException(404)
        expected = STATE / "evidence" / str(aid) / (str(evidence_id) + ".png")
        if (
            Path(step["screenshot_ref"]).resolve() != expected.resolve()
            or not expected.is_file()
        ):
            raise HTTPException(404)
    return FileResponse(expected, media_type="image/png")


@app.post("/api/agents/{aid}/messages", status_code=202)
def message(aid: UUID, body: Message):
    return domain.submit_message(
        str(aid),
        body.text.strip(),
        str(body.client_message_id),
        [str(x) for x in body.asset_ids],
    )


@app.post("/api/agents/{aid}/retry")
def retry(aid: UUID):
    domain.retry_messages(str(aid))
    return {"ok": True}


@app.post("/api/agents/{aid}/control")
async def control(aid: UUID, body: Control):
    await asyncio.to_thread(domain.set_control, str(aid), body.action, body.platform)
    if body.action in ("pause", "archive"):
        try:
            await gateway("/agents/" + str(aid) + "/close", timeout=20)
        except HTTPException:
            pass
    return {"ok": True}


@app.get("/api/agents/{aid}/community-growth")
def community_strategy(aid: UUID):
    from .community_growth import get_strategy

    return get_strategy(str(aid))


@app.put("/api/agents/{aid}/community-growth")
def community_strategy_update(aid: UUID, body: GrowthConfig):
    from .community_growth import set_strategy

    return set_strategy(str(aid), body.model_dump())


class CommunityResearchRequest(BaseModel):
    client_request_id: UUID


@app.post("/api/agents/{aid}/community-growth/research")
def community_research_request(aid: UUID, body: CommunityResearchRequest):
    from .community_growth import request_research

    return request_research(str(aid), str(body.client_request_id))


class TelegramLaunch(BaseModel):
    client_request_id: UUID


@app.post("/api/agents/{aid}/telegram/bootstrap")
def telegram_launch(aid: UUID, body: TelegramLaunch):
    from .account_setup import request_fresh_channel

    return request_fresh_channel(str(aid), str(body.client_request_id))


@app.get("/api/agents/{aid}/telegram/bootstrap/{task_id}")
def telegram_launch_status(aid: UUID, task_id: UUID):
    from .account_setup import launch_status

    return launch_status(str(aid), str(task_id))


@app.post("/api/agents/{aid}/telegram/bootstrap/{task_id}/recheck")
def telegram_launch_recheck(aid: UUID, task_id: UUID):
    from .account_setup import recheck_launch

    return recheck_launch(str(aid), str(task_id))


@app.get("/api/platforms")
def platforms():
    with tx() as c:
        caps = all_rows(c, "SELECT * FROM lead.platform_capabilities")
    return [
        {
            "id": p[0],
            "name": p[1],
            "automation": any(
                c["platform"] == p[0]
                and c["technical_state"] == "verified"
                and c["permission_state"] == "approved"
                for c in caps
            ),
            "status": "暂不开放"
            if any(
                c["platform"] == p[0] and c["permission_state"] == "restricted"
                for c in caps
            )
            else "待实号验证"
            if any(
                c["platform"] == p[0] and c["permission_state"] == "approved"
                for c in caps
            )
            else "待确认适用权限",
        }
        for p in domain.PLATFORMS
    ]


@app.post("/api/agents/{aid}/connect")
async def connect(aid: UUID, body: Connection):
    aid = str(aid)
    if body.platform not in [p[0] for p in domain.PLATFORMS] + ["lab"]:
        raise HTTPException(400, "频道不存在")
    await asyncio.to_thread(domain.set_control, aid, "pause")
    opened = (
        await gateway("/agents/" + aid + "/open", body={"platform": body.platform})
    ).json()
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        if one(
            c,
            "SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()",
        ):
            raise ValueError("此环境正在备份，请稍后重试")
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        if body.platform != "lab" and not one(
            c, "SELECT id FROM lead.social_accounts WHERE platform=%s", (body.platform,)
        ):
            c.execute(
                "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state) VALUES(%s,%s,%s,%s,'pending')",
                (current_tenant(), aid, profile["id"], body.platform),
            )
        c.execute(
            "UPDATE lead.takeover_sessions SET state='expired',ended_at=now() WHERE state IN ('requested','active','ending')"
        )
        takeover_token = token("takeover:" + aid, 1800)
        takeover = one(
            c,
            "INSERT INTO lead.takeover_sessions(tenant_id,agent_id,browser_session_id,profile_id,token_hash,control_version,state,expires_at) VALUES(%s,%s,%s,%s,%s,%s,'active',now()+interval '30 minutes') RETURNING id",
            (
                current_tenant(),
                aid,
                opened["session_id"],
                profile["id"],
                hashlib.sha256(takeover_token.encode()).hexdigest(),
                a["control_version"],
            ),
        )
        record(c, aid, "已打开账号登录窗口", body.platform)
        audit(c, aid, "browser.takeover.started", {"platform": body.platform})
    return {
        "takeover_id": takeover["id"],
        "platform": body.platform,
        "status": "pending",
        "automation": False,
    }


@app.post("/api/agents/{aid}/takeover/end")
def end_takeover(aid: UUID):
    aid = str(aid)
    with tx(aid) as c:
        require_agent(c, aid, True)
        c.execute(
            "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state IN ('requested','active','ending')"
        )
        record(c, aid, "已结束接管", "Agent 保持暂停。")
    return {"ok": True}


@app.get("/api/agents/{aid}/browser/screenshot")
async def screenshot(aid: UUID, format: Literal["png", "jpeg"] = "png"):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
    result = await gateway(
        "/agents/" + str(aid) + "/screenshot?format=" + format, "GET"
    )
    return Response(
        result.content, media_type="image/jpeg" if format == "jpeg" else "image/png"
    )


@app.post("/api/agents/{aid}/browser/input")
async def browser_input(aid: UUID, body: BrowserInput):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
    await gateway("/agents/" + str(aid) + "/input", body=body.model_dump())
    return {"ok": True}


@app.post("/api/agents/{aid}/lab/connect")
def connect_lab(aid: UUID):
    domain.connect_lab(str(aid))
    return {"ok": True}


@app.post("/api/agents/{aid}/lab/publish", status_code=202)
def publish_lab(aid: UUID, body: LabPublish):
    return domain.queue_lab_publish(str(aid), body.body, body.key)


@app.get("/api/agents/{aid}/lab/posts")
def lab_posts(aid: UUID):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        return all_rows(c, "SELECT * FROM lead.lab_posts ORDER BY created_at DESC")


def check_lab(request, aid):
    aid = str(UUID(aid))
    if not valid(request.cookies.get("lead_lab", ""), "lab:" + aid):
        raise HTTPException(403, "仅可在此 Agent 的浏览器环境中访问")
    return aid


@app.get("/lab/{aid}/identity/", response_class=HTMLResponse)
def identity_page(aid: str, request: Request):
    check_lab(request, aid)
    from .identity import page

    return page()


@app.get("/lab/{aid}/", response_class=HTMLResponse)
def lab_page(aid: str, request: Request):
    aid = check_lab(request, aid)
    with tx(aid) as c:
        agent = require_agent(c, aid)
        posts = all_rows(
            c, "SELECT * FROM lead.lab_posts ORDER BY created_at DESC LIMIT 30"
        )
    rendered = "".join(
        '<article data-post-key="'
        + html.escape(p["operation_key"], quote=True)
        + '"><p>'
        + html.escape(p["body"])
        + "</p><small>"
        + html.escape(str(p["created_at"]))
        + "</small></article>"
        for p in posts
    )
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>lead 本地测试站</title><style>body{font:16px system-ui;background:#f6f7f4;color:#292e29;max-width:740px;margin:60px auto}h1{font-size:26px}textarea{display:block;width:95%;height:130px;padding:15px;font:inherit;border:1px solid #cdd2c9;border-radius:8px}input{display:block;margin:10px 0;padding:8px;width:95%}button{margin:16px 0;padding:10px 28px;background:#303d32;color:#fafbf8;border:0;border-radius:7px}article{padding:20px 0;border-top:1px solid #ddd}small{color:#6c746c}p{white-space:pre-wrap}</style><h1>本地测试站</h1><p>'
        + html.escape(agent["name"])
        + '</p><form method="post"><label for="body">发布内容</label><textarea id="body" name="body" required></textarea><label for="operation_key">提交标识</label><input id="operation_key" name="operation_key" required><button type="submit">发布</button></form>'
        + rendered
        + "</html>"
    )


@app.post("/lab/{aid}/")
async def lab_submit(aid: str, request: Request):
    aid = check_lab(request, aid)
    if request.headers.get("origin") != "http://127.0.0.1:8088":
        raise HTTPException(403)
    form = await request.form()
    body = str(form.get("body", ""))
    key = str(form.get("operation_key", ""))
    if (
        not body.strip()
        or len(body) > 4000
        or not __import__("re").fullmatch(r"[A-Za-z0-9:_.-]{1,100}", key)
    ):
        raise HTTPException(400, "内容无效")
    with tx(aid) as c:
        require_agent(c, aid)
        # The fixture independently deduplicates accepted submissions.
        c.execute(
            "INSERT INTO lead.lab_posts(tenant_id,agent_id,operation_key,body) VALUES(%s,%s,%s,%s) ON CONFLICT(agent_id,operation_key) DO NOTHING",
            (current_tenant(), aid, key, body),
        )
    return RedirectResponse("/lab/" + aid + "/", status_code=303)


app.add_middleware(TelemetryMiddleware, service="lead-api")
