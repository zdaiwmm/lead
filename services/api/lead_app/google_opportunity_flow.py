"""Constrained Google sign-in in the Agent's separate browser profile."""

import asyncio
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException
from playwright.async_api import Error as BrowserError
from pydantic import BaseModel, ConfigDict

from .db import one, require_agent, tx
from .google_opportunity import (
    _policy,
    eligible_attempt,
    email_digest,
    https_origin,
    is_profile,
)

router = APIRouter(prefix="/agents/{aid}/google-opportunities")
ACCOUNTS = "accounts.google.com"
MY_ACCOUNT = "myaccount.google.com"
SETUP_URL = f"https://{MY_ACCOUNT}/?hl=en"
AUTH_PATHS = {"/o/oauth2/auth", "/o/oauth2/v2/auth"}
BASIC_SCOPES = {
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
}
GOOGLE_SIGNIN = re.compile(
    r"^(?:(?:sign in|log in|continue|sign up|register) with google|使用\s*Google\s*(?:账号|帐号)?\s*(?:登录|登入|继续)|Google\s*(?:登录|登入))$",
    re.I,
)
EMAIL = re.compile(r"[^\s<>@(),]+@[^\s<>@(),]+\.[a-z]+", re.I)
GOOGLE_ACCOUNT_LABEL = re.compile(
    r"^(?:Google Account|Google\s*(?:账号|帐号|帳戶))\s*[:：]", re.I
)
BLOCKED_PERMISSIONS = re.compile(
    r"gmail|google drive|calendar|contacts|read your email|access all|manage your|读取.*邮件|云端硬盘|通讯录|日历",
    re.I,
)
PAYMENT = re.compile(
    r"checkout|payment|billing|subscribe|purchase|充值|支付|付款", re.I
)


@dataclass(frozen=True)
class OAuthRequest:
    url: str
    callback: str
    state: str

    def accepts_return(self, url):
        parsed = urlsplit(url)
        base = urlunsplit(parsed._replace(query="", fragment=""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        return (
            base == self.callback
            and not parsed.fragment
            and query.get("state") == [self.state]
            and len(query.get("code", [])) == 1
            and bool(query["code"][0])
            and len(query.get("scope", [""])) == 1
            and "error" not in query
            and set(query.get("scope", [""])[0].split()) <= BASIC_SCOPES
        )


def read_oauth_request(url, site):
    """Only use the site's observed Google authorization request, unchanged."""
    if https_origin(url) != f"https://{ACCOUNTS}":
        return None, None
    parsed = urlsplit(url)
    if parsed.path not in AUTH_PATHS:
        return None, None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()):
        return None, "无法确认 Google 授权参数"
    params = {key: values[0] for key, values in query.items()}
    if set(params) - {
        "client_id",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
        "nonce",
        "prompt",
        "access_type",
        "code_challenge",
        "code_challenge_method",
        "hl",
        "login_hint",
        "include_granted_scopes",
        "select_account",
    }:
        return None, "Google 授权参数超出自动登录范围"
    callback = params.get("redirect_uri", "")
    if https_origin(callback) != site:
        return None, "Google 授权回调不在候选站点"
    callback_parts = urlsplit(callback)
    if (
        callback_parts.query
        or callback_parts.fragment
        or PAYMENT.search(callback_parts.path)
        or not re.fullmatch(
            r"[a-zA-Z0-9._-]+\.apps\.googleusercontent\.com",
            params.get("client_id", ""),
        )
        or params.get("response_type") != "code"
        or not params.get("state")
        or params.get("access_type", "online") != "online"
        or params.get("include_granted_scopes", "false") != "false"
    ):
        return None, "无法确认授权仅用于此站点登录"
    scopes = set(params.get("scope", "").split())
    if not scopes:
        return None, "无法读取 Google 请求的授权范围"
    if extra := scopes - BASIC_SCOPES:
        brief = "、".join(
            scope.removeprefix("https://www.googleapis.com/auth/")[:70]
            for scope in sorted(extra)[:4]
        )
        return None, "Google 请求了额外权限：" + brief
    return OAuthRequest(url, callback, params["state"]), None


class BrowserAuthority:
    """One profile operation at a time. The proxy also fences redirects."""

    def __init__(self, check, *, owner=False, site=None):
        self.check = check
        self.owner = owner
        self.site = site
        self.oauth = None
        self.returned = False
        self.callback_allowed = False
        self.post_available = False

    def control_token(self):
        # Also invalidate established CONNECT tunnels at the OAuth boundary.
        return self, self.oauth is not None, self.callback_allowed

    def host_allowed(self, host, port):
        if port != 443:
            return False
        if host == (urlsplit(self.site).hostname if self.site else None):
            return self.oauth is None or self.callback_allowed
        google = self.owner or self.oauth is not None
        return google and (
            host
            in {
                ACCOUNTS,
                MY_ACCOUNT,
                "ssl.gstatic.com",
                "www.gstatic.com",
                "fonts.gstatic.com",
            }
            or bool(host and re.fullmatch(r"lh[1-6]\.googleusercontent\.com", host))
        )

    def allow(self, url, method, *, navigation, resource_type):
        site = https_origin(url)
        if not site or not self.host_allowed(urlsplit(site).hostname, 443):
            return False
        parsed = urlsplit(url)
        if site == self.site:
            if navigation and not self.check():
                return False
            if method not in {"GET", "HEAD"} or PAYMENT.search(parsed.path):
                return False
            if self.oauth and navigation:
                if self.oauth.accepts_return(url):
                    if not self.check():
                        return False
                    if not self.callback_allowed:
                        return False
                elif not self.returned:
                    return False
            if self.oauth and not self.returned and not navigation:
                return False
            return True
        if parsed.hostname == ACCOUNTS:
            if not self.check():
                return False
            if self.owner:
                return method in {"GET", "HEAD", "POST"}
            if parsed.path in AUTH_PATHS:
                return method == "GET" and url == self.oauth.url
            if "scope" in parse_qs(parsed.query) or "redirect_uri" in parse_qs(
                parsed.query
            ):
                return False
            if method == "POST":
                if not self.post_available or not re.match(
                    r"/(?:signin/oauth/|_/[^/]+/data/batchexecute$)", parsed.path
                ):
                    return False
                self.post_available = False
                return True
            return method in {"GET", "HEAD"} and (
                parsed.path.startswith(
                    ("/signin/oauth/", "/v3/signin/accountchooser", "/o/oauth2/")
                )
                or not navigation
                and resource_type in {"script", "stylesheet", "image", "font"}
            )
        if parsed.hostname == MY_ACCOUNT:
            return self.owner and method in {"GET", "HEAD"} and self.check()
        return (
            not navigation
            and method in {"GET", "HEAD"}
            and resource_type in {"script", "stylesheet", "image", "font"}
        )


def _binding(c, profile_id, revision, *, lock=False):
    row = _policy(c, lock=lock)
    if (
        not row
        or str(row["profile_id"]) != str(profile_id)
        or row["revision"] != revision
    ):
        return None
    return row


def _takeover_check(aid, pid, revision, takeover=None):
    with tx(aid) as c:
        row = _binding(c, pid, revision)
        if not row or row["state"] != "needs_owner" or not takeover:
            return False
        return bool(
            one(
                c,
                """SELECT 1 FROM lead.takeover_sessions t JOIN lead.agents a ON a.id=t.agent_id
            JOIN lead.profile_leases l ON l.profile_id=t.profile_id
            JOIN lead.browser_sessions b ON b.id=t.browser_session_id AND b.epoch=l.epoch
            WHERE t.id=%s AND t.profile_id=%s AND t.state='active' AND t.expires_at>now()
            AND a.desired_state<>'archived' AND a.control_version=t.control_version
            AND b.ended_at IS NULL AND l.state='held' AND l.expires_at>now()""",
                (takeover, pid),
            )
        )


async def _actor(aid, pid):
    from .browser import gateway

    aid = str(aid)
    with tx(aid) as c:
        require_agent(c, aid)
        profile = one(c, "SELECT * FROM lead.browser_profiles WHERE id=%s", (pid,))
        if not profile or not is_profile(profile):
            raise HTTPException(409, "Google 浏览器资料已变更")
    return await gateway.get(aid, pid, allow_challenge=True)


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: UUID


class OpenRequest(ProfileRequest):
    revision: int


class VerifyRequest(OpenRequest):
    takeover: UUID


class AttemptRequest(ProfileRequest):
    task_id: UUID
    url: str
    observed_url: str
    evidence_id: UUID | None = None


@router.post("/open")
async def open_setup(aid: UUID, body: OpenRequest):
    from .browser import manual_control

    with tx(aid) as c:
        if not _binding(c, body.profile_id, body.revision):
            raise HTTPException(409, "Google 浏览器资料已变更")
    actor = await _actor(aid, body.profile_id)
    async with manual_control(actor):
        with tx(aid) as c:
            agent = require_agent(c, aid, True)
            if not _binding(c, body.profile_id, body.revision, lock=True):
                raise HTTPException(409, "Google 浏览器资料已变更")
            row = one(
                c,
                """INSERT INTO lead.takeover_sessions(tenant_id,agent_id,browser_session_id,
                profile_id,token_hash,control_version,state,expires_at)
                VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,'active',now()+interval '20 minutes')
                RETURNING id""",
                (
                    aid,
                    actor.session,
                    body.profile_id,
                    secrets.token_hex(32),
                    agent["control_version"],
                ),
            )
        takeover = row["id"]
        actor.google_authority = BrowserAuthority(
            lambda: _takeover_check(aid, body.profile_id, body.revision, takeover),
            owner=True,
        )
        try:
            await actor.page.goto(
                SETUP_URL, wait_until="domcontentloaded", timeout=22000
            )
        except BrowserError:
            # Retain the original screen so the owner can see Google's response.
            pass
        actor.touch()
    return {"takeover_id": str(takeover), "status": "pending"}


async def google_identity(page, digest):
    if https_origin(page.url) != f"https://{MY_ACCOUNT}":
        return False
    labels = await page.locator("[aria-label]").evaluate_all(
        "nodes => nodes.filter(node => node.getClientRects().length).map(node => node.getAttribute('aria-label'))"
    )
    labels = [label for label in labels if GOOGLE_ACCOUNT_LABEL.match(label or "")]
    observed = {
        email_digest(address) for label in labels for address in EMAIL.findall(label)
    }
    return observed == {digest}


@router.post("/verify")
async def verify_setup(aid: UUID, body: VerifyRequest):
    from .browser import manual_control, require_takeover

    actor = await _actor(aid, body.profile_id)
    async with manual_control(actor):
        require_takeover(str(aid), actor, body.takeover)
        if not _takeover_check(aid, body.profile_id, body.revision, body.takeover):
            raise HTTPException(409, "请重新打开 Google 登录窗口")
        with tx(aid) as c:
            row = _binding(c, body.profile_id, body.revision)
        if not row:
            raise HTTPException(409, "Google 浏览器资料已变更")
        if https_origin(actor.page.url) == f"https://{ACCOUNTS}":
            raise HTTPException(409, "请在原窗口完成 Google 登录和本人验证")
        if not await google_identity(actor.page, row["email_digest"]):
            raise HTTPException(
                409, "未核验到指定 Google 邮箱，请切换至该账号的 Google 账号主页"
            )
        with tx(aid) as c:
            require_agent(c, aid, True)
            if not _binding(c, body.profile_id, body.revision, lock=True) or not one(
                c,
                """UPDATE lead.takeover_sessions
                SET state='ended',ended_at=now() WHERE id=%s AND state='active' AND expires_at>now()
                AND NOT input_inflight AND NOT composing RETURNING id""",
                (body.takeover,),
            ):
                raise HTTPException(409, "Google 登录窗口已变更")
            c.execute("UPDATE lead.google_opportunity_policies SET state='ready'")
        actor.google_authority = None
        return {"status": "ready"}


@router.post("/close")
async def close_profile(aid: UUID, body: ProfileRequest):
    from .browser import gateway

    with tx(aid) as c:
        profile = one(
            c, "SELECT * FROM lead.browser_profiles WHERE id=%s", (body.profile_id,)
        )
        row = _policy(c)
        if (
            not profile
            or not is_profile(profile)
            or row
            and row["profile_id"] == body.profile_id
        ):
            raise HTTPException(409, "Google 浏览器资料仍在使用")
    await gateway.close_one(str(aid), str(body.profile_id))
    return {"ok": True}


def _still_authorized(aid, pid, revision, task_id, control_version, attempt_id):
    with tx(aid) as c:
        row = _binding(c, pid, revision)
        return bool(
            row
            and row["enabled"]
            and row["state"] == "ready"
            and one(
                c,
                """
            SELECT 1 FROM lead.google_opportunity_attempts p JOIN lead.tasks t ON t.id=p.task_id
            JOIN lead.agents a ON a.id=p.agent_id
            JOIN lead.growth_experiments e ON e.id=t.experiment_id
            WHERE e.state='active' AND e.specification->>'question_type'='distribution'
            AND p.id=%s AND p.status='checking' AND t.id=%s AND t.state='running'
            AND a.desired_state='running' AND a.control_version=%s
            AND NOT EXISTS(SELECT FROM lead.takeover_sessions x WHERE x.profile_id=p.profile_id
                AND x.state='active' AND x.expires_at>now())""",
                (attempt_id, task_id, control_version),
            )
        )


def _claim(aid, body):
    site = https_origin(body.url)
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        row = _policy(c, lock=True)
        if (
            not row
            or not row["enabled"]
            or row["state"] != "ready"
            or row["profile_id"] != body.profile_id
        ):
            return None, {"status": "disabled"}
        if not eligible_attempt(
            c, aid, body.task_id, body.evidence_id, body.url, body.observed_url
        ):
            return None, {
                "status": "unsupported",
                "reason": "未取得此站点的当前公开浏览证据",
            }
        previous = one(
            c,
            """SELECT * FROM lead.google_opportunity_attempts WHERE profile_id=%s AND origin=%s
            FOR UPDATE""",
            (body.profile_id, site),
        )
        # A completed callback, or a crash after dispatch, can already have
        # created the account. Never replay it on a later research pass.
        if previous and previous["status"] == "checking":
            if (
                previous["dispatched"]
                or previous["updated_at"].timestamp() + 60 > time.time()
            ):
                return None, _outcome("unverified", "原授权尝试仍待核验")
        elif previous and (
            previous["dispatched"] or previous["revision"] == row["revision"]
        ):
            status = previous["status"]
            return None, {"status": status, "reason": previous["reason"]}
        status = "checking" if row["allow_free_signup"] else "needs_owner"
        reason = (
            None
            if status == "checking"
            else "首次使用 Google 登录可能建号，请先开启允许免费建号"
        )
        claimed = one(
            c,
            """INSERT INTO lead.google_opportunity_attempts
            (tenant_id,agent_id,profile_id,task_id,origin,revision,status,reason)
            VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(profile_id,origin) DO UPDATE SET task_id=EXCLUDED.task_id,
                revision=EXCLUDED.revision,status=EXCLUDED.status,reason=EXCLUDED.reason,updated_at=now()
            RETURNING id""",
            (aid, body.profile_id, body.task_id, site, row["revision"], status, reason),
        )
        if status != "checking":
            return None, _outcome(status, reason)
        return {
            **row,
            "attempt_id": claimed["id"],
            "site": site,
            "control_version": agent["control_version"],
        }, None


def _outcome(status, reason=None):
    return {"status": status, "reason": reason}


async def _google_link(page, site):
    matches = page.get_by_role("link", name=GOOGLE_SIGNIN)
    urls = set()
    for item in (await matches.all())[:8]:
        if await item.is_visible():
            address = await item.evaluate("link => link.href")
            if https_origin(address) == f"https://{ACCOUNTS}":
                urls.add(address)
    if len(urls) == 1:
        return read_oauth_request(urls.pop(), site)
    if (
        urls
        or await page.get_by_role("button", name=GOOGLE_SIGNIN).count()
        or await matches.count()
    ):
        return None, "此站点的 Google 登录方式需要本人接管"
    return None, None


async def _select_account(page, digest):
    matches = []
    for item in (await page.locator("[data-email], [data-identifier]").all())[:20]:
        if not await item.is_visible():
            continue
        address = (
            await item.get_attribute("data-email")
            or await item.get_attribute("data-identifier")
            or ""
        )
        if email_digest(address) == digest:
            matches.append(item)
    return matches[0] if len(matches) == 1 else None


async def _first_party_identity(page, digest, site):
    if https_origin(page.url) != site:
        return False
    controls = page.locator(
        '[data-user-email], [aria-label^="Account:"], [aria-label^="Signed in as"], [rel="me"]'
    )
    identities = await controls.evaluate_all("""nodes => nodes.filter(node => node.getClientRects().length)
        .map(node => node.getAttribute('data-user-email') || node.getAttribute('aria-label') || node.innerText || '')""")
    addresses = {
        email_digest(address)
        for value in identities
        for address in EMAIL.findall(value)
    }
    return addresses == {digest}


async def _connected_observation(page, subject, site):
    if not await _first_party_identity(page, subject, site):
        return _outcome("unverified", "已返回平台，尚未核验到当前登录身份")
    clean = urlunsplit(urlsplit(page.url)._replace(query="", fragment=""))
    text = await page.evaluate("""() => {
        const root = document.querySelector('main, [role="main"], article');
        if (!root) return '';
        const clone = root.cloneNode(true);
        for (const node of clone.querySelectorAll('form, script, style, nav, [hidden], [aria-hidden="true"], [data-user-email]')) {
            node.remove();
        }
        return clone.textContent.trim().slice(0, 8000);
    }""")
    result = _outcome("connected")
    if len(text) >= 40:
        result["observation"] = {
            "url": clean,
            "title": EMAIL.sub("[email]", (await page.title())[:180]),
            "text": EMAIL.sub("[email]", text),
        }
    return result


async def _use_google(actor, page, body, claim, authority):
    site = claim["site"]
    await page.goto(body.observed_url, wait_until="domcontentloaded", timeout=8500)
    if https_origin(page.url) != site:
        return _outcome("unsupported", "候选站点跳转到了其他域名")
    if await _first_party_identity(page, claim["email_digest"], site):
        return _outcome("unverified", "候选站点已显示账号，缺少本次 Google 授权回执")
    oauth, reason = await _google_link(page, site)
    if not oauth:
        return _outcome("needs_owner" if reason else "no_google_login", reason)
    if not authority.check():
        return _outcome("disabled")
    hint = parse_qs(urlsplit(oauth.url).query).get("login_hint")
    if hint and email_digest(hint[0]) != claim["email_digest"]:
        return _outcome("needs_owner", "站点要求使用不同的 Google 登录身份")
    authority.oauth = oauth

    async def click_google(control):
        if not authority.check():
            return False
        with tx(actor.aid) as c:
            c.execute(
                "UPDATE lead.google_opportunity_attempts SET dispatched=true WHERE id=%s",
                (claim["attempt_id"],),
            )
        authority.callback_allowed = True
        authority.post_available = True
        try:
            await control.click(timeout=2500)
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        finally:
            authority.post_available = False
        return True

    def note_response(response):
        # Playwright can follow server redirects without repeating route().
        request = response.request
        previous = request.redirected_from
        if (
            authority.callback_allowed
            and request.is_navigation_request()
            and request.frame == page.main_frame
            and previous
            and https_origin(previous.url) == f"https://{ACCOUNTS}"
            and 200 <= response.status < 400
            and oauth.accepts_return(response.url)
            and authority.check()
        ):
            authority.returned = True

    page.on("response", note_response)
    try:
        await page.goto(oauth.url, wait_until="domcontentloaded", timeout=8500)
    except BrowserError:
        if not authority.callback_allowed:
            return _outcome("needs_owner", "Google 未显示可核验的账号或授权页面")
        raise
    attempted_stage = set()
    for _ in range(3):
        if not authority.check():
            return _outcome("disabled")
        if authority.returned:
            return await _connected_observation(page, claim["email_digest"], site)
        if https_origin(page.url) != f"https://{ACCOUNTS}":
            break
        path = urlsplit(page.url).path
        if path.startswith(("/signin/oauth/consent", "/signin/oauth/id")):
            stage = "consent"
            text = await page.locator("body").inner_text(timeout=1500)
            if BLOCKED_PERMISSIONS.search(text) or PAYMENT.search(text):
                return _outcome("needs_owner", "授权页显示了额外权限或付费条款")
            if not any(
                email_digest(address) == claim["email_digest"]
                for address in EMAIL.findall(text)
            ):
                break
            control = page.get_by_role(
                "button", name=re.compile(r"^(Continue|Allow|继续|允许)$", re.I)
            )
            if await control.count() != 1 or not await control.is_visible():
                break
        elif "accountchooser" in path.lower():
            stage = "chooser"
            control = await _select_account(page, claim["email_digest"])
            if not control:
                break
        else:
            break
        if stage in attempted_stage:
            break
        attempted_stage.add(stage)
        if not await click_google(control):
            return _outcome("disabled")
    if authority.returned and authority.check():
        return await _connected_observation(page, claim["email_digest"], site)
    return _outcome("needs_owner", "Google 需要验证本人，或当前登录流程无法自动确认")


@router.post("/attempt")
async def attempt(aid: UUID, body: AttemptRequest):
    from .browser import BrowserBusy

    claim, cached = await asyncio.to_thread(_claim, aid, body)
    if not claim:
        return cached
    outcome = _outcome("unverified", "浏览器通信中断，授权结果尚未核验")
    try:
        async with asyncio.timeout(26):
            actor = await _actor(aid, body.profile_id)
            async with actor.lock:
                authority = BrowserAuthority(
                    lambda: _still_authorized(
                        aid,
                        body.profile_id,
                        claim["revision"],
                        body.task_id,
                        claim["control_version"],
                        claim["attempt_id"],
                    ),
                    site=claim["site"],
                )
                if not authority.check():
                    outcome = _outcome("disabled")
                else:
                    actor.google_authority = authority
                    page = None
                    try:
                        page = await actor.context.new_page()
                        outcome = await _use_google(actor, page, body, claim, authority)
                    finally:
                        actor.google_authority = None
                        if page and not page.is_closed():
                            await page.close()
                        actor.touch()
    except (BrowserBusy, BrowserError, TimeoutError):
        pass
    finally:
        with tx(aid) as c:
            c.execute(
                """UPDATE lead.google_opportunity_attempts SET status=%s,reason=%s,updated_at=now()
                WHERE id=%s AND status='checking'""",
                (outcome["status"], outcome["reason"], claim["attempt_id"]),
            )
    return outcome
