"""Owner-authorized, account-bound browser acceptance, never global approval."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .db import audit, emit, j, one, require_agent, tx
from .scope import principal

router = APIRouter()


class PilotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: UUID
    expires_at: datetime
    source_quote: str = Field(min_length=5, max_length=1000)


@router.post("/api/agents/{aid}/accounts/browser-pilot")
async def enable(aid: UUID, body: PilotInput):
    if (principal() or {}).get("role") != "owner":
        raise HTTPException(403, "仅工作室所有者可明确授权浏览器实跑")
    now = datetime.now(timezone.utc)
    if not body.expires_at.tzinfo or not now < body.expires_at <= now + timedelta(
        hours=24
    ):
        raise HTTPException(422, "请指定未来24小时以内的本次验收结束时间")
    aid = str(aid)
    with tx(aid) as c:
        a = require_agent(c, aid)
        target = one(
            c,
            "SELECT t.*,s.platform,s.state account_state,s.external_account_id,s.automation_enabled,p.permission_state FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id JOIN lead.platform_capabilities p ON p.platform=s.platform AND p.target_type=t.target_type AND p.action_type='publish' WHERE t.id=%s",
            (body.target_id,),
        )
        if (
            not target
            or target["platform"] != "tiktok"
            or target["permission_state"] not in ("pending", "approved")
            or target["state"] != "active"
            or target["account_state"] != "connected"
            or not target["automation_enabled"]
            or a["desired_state"] != "running"
        ):
            raise HTTPException(409, "当前账号、平台状态或运行范围不适合本次浏览器验收")
        previous_identity = target["external_account_id"]
    from .activities import browser_rpc

    observed = await browser_rpc(
        "/agents/" + aid + "/target/inspect",
        {"account_id": str(target["account_id"]), "url": target["canonical_url"]},
    )
    if str(observed.get("external_target_id")) != str(target["external_target_id"]):
        raise HTTPException(409, "实际浏览器目标与已绑定目标不同")
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        current = one(
            c,
            "SELECT external_account_id,state FROM lead.social_accounts WHERE id=%s",
            (target["account_id"],),
        )
        grant = one(
            c,
            "SELECT id FROM lead.authorization_grants WHERE target_id=%s AND revoked_at IS NULL AND allowed_actions ? 'publish' AND allowed_actions ? 'upload_media' ORDER BY version DESC LIMIT 1",
            (target["id"],),
        )
        if (
            current["state"] != "connected"
            or current["external_account_id"] != previous_identity
            or not grant
            or a["desired_state"] != "running"
        ):
            raise HTTPException(409, "登录身份或现有发布授权已改变")
        pilots = a["onboarding_state"].get("browser_pilots", {})
        value = {
            "authority": "workspace_owner",
            "method": "browser",
            "state": "active",
            "account_id": str(target["account_id"]),
            "external_account_id": previous_identity,
            "target_id": str(target["id"]),
            "grant_id": str(grant["id"]),
            "expires_at": body.expires_at.isoformat(),
            "source_quote": body.source_quote,
            "authorized_at": now.isoformat(),
        }
        pilots[str(target["id"])] = value
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s,version=version+1 WHERE id=%s",
            (j({"browser_pilots": pilots}), aid),
        )
        audit(c, aid, "browser.pilot.authorized", value)
        emit(c, aid, "supervisor.wake")
    return {"ok": True, "pilot": value, "formats_verified": False}
