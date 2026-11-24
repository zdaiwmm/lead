"""Owner policy and immutable public evidence for opportunity sign-in."""

import asyncio
import hashlib
import re
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .admin_api import require_owner
from .db import all_rows, audit, one, require_agent, tx, uid

PROFILE_PREFIX = "google-opportunity-"
router = APIRouter(
    prefix="/api/agents/{aid}/google-opportunities",
    dependencies=[Depends(require_owner)],
)


def email_digest(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def is_profile(profile):
    return profile["slot"].startswith(PROFILE_PREFIX)


def is_google_takeover(c, takeover, *, include_active=False):
    if not takeover and not include_active:
        return False
    return bool(
        one(
            c,
            """SELECT 1 FROM lead.takeover_sessions t
        JOIN lead.browser_profiles p ON p.id=t.profile_id
        WHERE p.slot LIKE %s AND (t.id=%s OR (%s::uuid IS NULL
        AND t.state IN ('requested','active','ending') AND t.expires_at>now())) LIMIT 1""",
            (PROFILE_PREFIX + "%", takeover, takeover),
        )
    )


def check_takeover_owner(c, takeover, *, include_active=False):
    if is_google_takeover(c, takeover, include_active=include_active):
        require_owner()


def https_origin(url):
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\\\x00-\x20]", url):
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and not (parsed.username or parsed.password)
            and parsed.hostname
            and re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*",
                parsed.hostname,
            )
        ):
            return "https://" + parsed.hostname
    except ValueError:
        pass
    return None


def _policy(c, *, lock=False):
    return one(
        c,
        "SELECT * FROM lead.google_opportunity_policies"
        + (" FOR UPDATE" if lock else ""),
    )


def automatic_enabled(c):
    row = _policy(c)
    return bool(row and row["enabled"] and row["state"] == "ready")


def _status(c):
    row = _policy(c)
    if not row:
        return dict(
            enabled=False,
            allow_free_signup=False,
            account_hint=None,
            state="unbound",
            attempts=[],
        )
    attempts = all_rows(
        c,
        """SELECT origin,
            CASE WHEN status='checking' AND updated_at<now()-interval '1 minute' THEN 'unverified' ELSE status END AS status,
            CASE WHEN status='checking' AND updated_at<now()-interval '1 minute' THEN '原授权尝试结果尚未核验' ELSE reason END AS reason,
            updated_at FROM lead.google_opportunity_attempts WHERE profile_id=%s
        ORDER BY updated_at DESC LIMIT 20""",
        (row["profile_id"],),
    )
    return {
        **{
            key: row[key]
            for key in ("enabled", "allow_free_signup", "account_hint", "state")
        },
        "attempts": attempts,
    }


def _slot(c, aid, agent, digest):
    from .account_profiles import directory

    row = _policy(c, lock=True)
    if row and row["email_digest"] == digest:
        return row["profile_id"], row["revision"], None
    base = one(c, "SELECT * FROM lead.browser_profiles WHERE slot='default'")
    if not base:
        raise HTTPException(409, "此 Agent 的浏览器环境尚未就绪")
    pid = uid()
    slot = PROFILE_PREFIX + pid
    root = directory(aid, {"slot": slot, "id": pid})
    c.execute(
        """INSERT INTO lead.browser_profiles(id,tenant_id,agent_id,provider,engine_version,sdk_version,
        config_ref,config_hash,storage_ref,key_material_ref,network_identity_id,slot)
        VALUES(%s,lead.ctx_tenant(),%s,'camoufox',%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            pid,
            aid,
            base["engine_version"],
            base["sdk_version"],
            str(root / "identity.json"),
            pid,
            str(root / "storage"),
            base["key_material_ref"],
            base["network_identity_id"],
            slot,
        ),
    )
    c.execute(
        """INSERT INTO lead.profile_leases(profile_id,tenant_id,agent_id,control_version)
        VALUES(%s,lead.ctx_tenant(),%s,%s)""",
        (pid, aid, agent["control_version"]),
    )
    return pid, row["revision"] if row else 0, row["profile_id"] if row else None


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)
    allow_free_signup: bool = Field(strict=True)


class BindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    takeover: UUID


@router.get("")
def settings(aid: UUID):
    with tx(aid) as c:
        require_agent(c, aid)
        return _status(c)


@router.put("")
def update(aid: UUID, body: PolicyChange):
    with tx(aid) as c:
        require_agent(c, aid, True)
        row = _policy(c, lock=True)
        if body.enabled and (not row or row["state"] != "ready"):
            raise HTTPException(409, "请先核验专用 Google 账号")
        c.execute(
            """INSERT INTO lead.google_opportunity_policies(tenant_id,agent_id,enabled,allow_free_signup)
            VALUES(lead.ctx_tenant(),%s,%s,%s) ON CONFLICT(agent_id) DO UPDATE
            SET enabled=EXCLUDED.enabled,allow_free_signup=EXCLUDED.allow_free_signup,
                revision=google_opportunity_policies.revision+1
            WHERE (google_opportunity_policies.enabled,google_opportunity_policies.allow_free_signup)
                IS DISTINCT FROM (EXCLUDED.enabled,EXCLUDED.allow_free_signup)""",
            (aid, body.enabled, body.allow_free_signup),
        )
        audit(c, aid, "opportunity.google.policy", body.model_dump())
        return _status(c)


async def browser_call(aid, profile_id, operation, payload, *, timeout=25):
    from .execution_routes import endpoint, resolve
    from .security import internal_token

    route = await asyncio.to_thread(resolve, str(aid), profile_id=profile_id)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(
            endpoint(route) + f"/agents/{aid}/google-opportunities/{operation}",
            json={"profile_id": str(profile_id), **payload},
            headers={"Authorization": "Bearer " + internal_token()},
        )
    if response.status_code == 409:
        raise HTTPException(
            409, response.json().get("detail", "浏览器暂无法完成 Google 登录")
        )
    response.raise_for_status()
    return response.json()


def _expire_takeover(c, pid):
    c.execute(
        """UPDATE lead.takeover_sessions SET state='ended',ended_at=now()
        WHERE profile_id=%s AND state IN ('requested','active','ending')""",
        (pid,),
    )


def _bind(aid, email):
    from .execution_routes import default_transport, register

    digest = email_digest(email)
    user, host = email.rsplit("@", 1)
    hint = f"{user[:1]}***@{host.lower()}"
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        pid, revision, previous = _slot(c, str(aid), agent, digest)
        if previous:
            _expire_takeover(c, previous)
        _expire_takeover(c, pid)
        c.execute(
            """INSERT INTO lead.google_opportunity_policies(tenant_id,agent_id,profile_id,email_digest,
            account_hint,state,revision) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,'needs_owner',%s)
            ON CONFLICT(agent_id) DO UPDATE SET profile_id=EXCLUDED.profile_id,email_digest=EXCLUDED.email_digest,
            account_hint=EXCLUDED.account_hint,state='needs_owner',revision=EXCLUDED.revision""",
            (aid, pid, digest, hint, revision + 1),
        )
        audit(c, aid, "opportunity.google.bind", {"profile_id": str(pid)})
    register(str(aid), pid, default_transport("google-opportunities"))
    return pid, revision + 1, previous


async def _owner_call(aid, profile_id, operation, payload):
    try:
        return await browser_call(aid, profile_id, operation, payload, timeout=37)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Google 专用浏览器暂不可用") from None


@router.post("/bind")
async def bind(aid: UUID, body: BindRequest):
    pid, revision, previous = await asyncio.to_thread(_bind, aid, body.email)
    if previous:
        await _close_previous(aid, previous)
    return await _owner_call(aid, pid, "open", {"revision": revision})


@router.post("/verify")
async def verify(aid: UUID, body: VerifyRequest):
    with tx(aid) as c:
        require_agent(c, aid)
        row = _policy(c)
        if not row or not row["profile_id"]:
            raise HTTPException(409, "请重新打开 Google 登录窗口")
    await _owner_call(
        aid,
        row["profile_id"],
        "verify",
        {"takeover": str(body.takeover), "revision": row["revision"]},
    )
    return settings(aid)


async def _close_previous(aid, pid):
    try:
        await browser_call(aid, pid, "close", {}, timeout=8)
    except (HTTPException, httpx.HTTPError, ValueError):
        # Revocation has already been committed. The idle gateway will close it.
        pass


@router.post("/disconnect")
async def disconnect(aid: UUID):
    with tx(aid) as c:
        require_agent(c, aid, True)
        row = _policy(c, lock=True)
        if row and row["profile_id"]:
            _expire_takeover(c, row["profile_id"])
            c.execute("""UPDATE lead.google_opportunity_policies SET enabled=false,state='unbound',
                profile_id=NULL,email_digest=NULL,account_hint=NULL,revision=revision+1""")
            audit(c, aid, "opportunity.google.disconnect")
        status = _status(c)
    if row and row["profile_id"]:
        await _close_previous(aid, row["profile_id"])
    return status


def eligible_attempt(c, aid, task_id, evidence_id, url, observed_url):
    from .growth.tools import opportunity_login_receipt

    site = https_origin(url)
    if not site or site != https_origin(observed_url) or not evidence_id:
        return False
    result = one(
        c,
        """SELECT s.output FROM lead.browser_steps s
        JOIN lead.browser_runs r ON r.id=s.run_id
        JOIN lead.tasks t ON t.id=r.task_id AND (t.tenant_id,t.agent_id)=(r.tenant_id,r.agent_id)
        JOIN lead.agents a ON a.id=t.agent_id
        JOIN lead.growth_experiments e ON e.id=t.experiment_id AND (e.tenant_id,e.agent_id)=(t.tenant_id,t.agent_id)
        JOIN lead.browser_profiles p ON p.id=r.profile_id
        WHERE t.id=%s AND t.agent_id=%s AND t.type='growth.research' AND t.state='running'
        AND t.input#>>'{spec,purpose}' IS DISTINCT FROM 'product'
        AND coalesce(t.input#>>'{spec,payload,access_mode}', 'public')='public'
        AND e.state='active' AND e.specification->>'question_type'='distribution'
        AND a.desired_state='running' AND s.id=%s AND s.state='completed'
        AND ((s.operation='open' AND s.input->>'url'=%s)
            OR (s.operation='read' AND s.input->>'opened_url'=%s))
        AND s.output->>'url'=%s AND s.output->>'evidence_id'=s.id::text
        AND r.mode='autonomous' AND p.slot IN ('native-public-research','public-research')""",
        (task_id, aid, evidence_id, url, url, observed_url),
    )
    return bool(result and opportunity_login_receipt(url, result["output"]))


async def try_opportunity_sign_in(aid, *, task_id, url, observed_url, evidence_id=None):
    with tx(aid) as c:
        row = _policy(c)
        if not row or not row["enabled"] or row["state"] != "ready":
            return {"status": "disabled"}
    try:
        return await browser_call(
            aid,
            row["profile_id"],
            "attempt",
            {
                "task_id": str(task_id),
                "url": url,
                "observed_url": observed_url,
                "evidence_id": str(evidence_id) if evidence_id else None,
            },
            timeout=34,
        )
    except (HTTPException, httpx.HTTPError, ValueError):
        return {"status": "unavailable", "reason": "Google 专用浏览器暂不可用"}
