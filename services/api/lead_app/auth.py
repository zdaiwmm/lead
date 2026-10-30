"""Authorization Code + PKCE OIDC login; only opaque sessions reach the browser."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import traceback
from urllib.parse import urlsplit
from uuid import UUID

import httpx2 as httpx
import jwt
from authlib.integrations.httpx_client import AsyncOAuth2Client
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import CONFIG, env
from .identity_store import auth_tx, login, sha
from .scope import principal

router = APIRouter()


def auth_mode():
    value = env().get("LEAD_AUTH_MODE") or "local_owner"
    if value not in ("local_owner", "oidc"):
        raise ValueError("登录模式配置不可用")
    return value


def public_origin():
    value = (env().get("LEAD_PUBLIC_URL") or "http://127.0.0.1:3100").rstrip("/")
    p = urlsplit(value)
    if (
        p.scheme not in ("http", "https")
        or not p.hostname
        or p.username
        or p.password
        or p.path not in ("", "/")
        or p.query
        or p.fragment
    ):
        raise ValueError("平台地址配置不可用")
    return value


def settings():
    values = env()
    issuer = (values.get("LEAD_OIDC_ISSUER") or "").strip()
    client_id = values.get("LEAD_OIDC_CLIENT_ID")
    secret = values.get("LEAD_OIDC_CLIENT_SECRET")
    if auth_mode() != "oidc" or not issuer or not client_id or not secret:
        raise HTTPException(503, "登录服务尚未配置")
    endpoint(issuer)
    origin = public_origin()
    if values.get("LEAD_DEPLOYMENT_MODE") == "production" and not origin.startswith(
        "https://"
    ):
        raise HTTPException(503, "登录服务需要 HTTPS 地址")
    return {
        "issuer": issuer,
        "client_id": client_id,
        "client_secret": secret,
        "redirect_uri": origin + "/api/auth/callback",
    }


def endpoint(value):
    p = urlsplit(value)
    local_dev = (
        env().get("LEAD_DEPLOYMENT_MODE") != "production"
        and env().get("LEAD_OIDC_ALLOW_HTTP_LOOPBACK") == "1"
        and p.hostname in ("localhost", "127.0.0.1")
    )
    if (
        not p.hostname
        or p.username
        or p.password
        or p.fragment
        or (p.scheme != "https" and not (local_dev and p.scheme == "http"))
    ):
        raise HTTPException(503, "登录服务地址不可用")
    return value


def oauth_client(cfg, transport=None):
    return AsyncOAuth2Client(
        cfg["client_id"],
        cfg["client_secret"],
        redirect_uri=cfg["redirect_uri"],
        scope="openid profile email",
        code_challenge_method="S256",
        timeout=15,
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    )


def public_client(transport=None):
    return httpx.AsyncClient(
        timeout=10, trust_env=False, follow_redirects=False, transport=transport
    )


async def bounded_json(url):
    async with (
        public_client() as client,
        client.stream("GET", endpoint(url)) as response,
    ):
        response.raise_for_status()
        chunks = []
        length = 0
        async for chunk in response.aiter_bytes():
            length += len(chunk)
            if length > 1024**2:
                raise ValueError("Identity response too large")
            chunks.append(chunk)
    result = json.loads(b"".join(chunks))
    if not isinstance(result, dict):
        raise ValueError("Identity response invalid")
    return result


async def metadata(cfg):
    result = await bounded_json(
        cfg["issuer"].rstrip("/") + "/.well-known/openid-configuration"
    )
    if result.get("issuer") != cfg["issuer"]:
        raise ValueError("Issuer mismatch")
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        endpoint(result[field])
    if "S256" not in result.get("code_challenge_methods_supported", []):
        raise ValueError("Provider must support PKCE S256")
    return result


def state_cipher():
    return AESGCM(
        hashlib.sha256((CONFIG["session_secret"] + ":oidc-state-v1").encode()).digest()
    )


def store_flow(state, browser, payload):
    nonce = secrets.token_bytes(12)
    encrypted = nonce + state_cipher().encrypt(
        nonce, json.dumps(payload).encode(), sha(state).encode()
    )
    with auth_tx() as c:
        c.execute(
            "SELECT lead.auth_begin(%s,%s,%s)", (sha(state), sha(browser), encrypted)
        )


def consume_flow(state, browser):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", state or "") or not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", browser or ""
    ):
        raise ValueError("Invalid state")
    with auth_tx() as c:
        value = c.execute(
            "SELECT lead.auth_consume(%s,%s) AS payload", (sha(state), sha(browser))
        ).fetchone()["payload"]
    if not value:
        raise ValueError("Expired, foreign or reused state")
    raw = bytes(value)
    return json.loads(state_cipher().decrypt(raw[:12], raw[12:], sha(state).encode()))


async def validate_id_token(raw, cfg, discovery, nonce):
    if not isinstance(raw, str) or len(raw) > 32768:
        raise ValueError("Invalid ID token")
    header = jwt.get_unverified_header(raw)
    allowed = {"RS256", "ES256"} & set(
        discovery.get("id_token_signing_alg_values_supported", [])
    )
    if header.get("alg") not in allowed:
        raise ValueError("Unsupported signing algorithm")
    keys = (await bounded_json(discovery["jwks_uri"])).get("keys", [])
    if not isinstance(keys, list) or len(keys) > 50:
        raise ValueError("Invalid key set")
    matches = [
        key
        for key in keys
        if key.get("kid") == header.get("kid")
        and key.get("use", "sig") == "sig"
        and key.get("kty") in ("RSA", "EC")
        and key.get("alg", header["alg"]) == header["alg"]
    ]
    if len(matches) != 1:
        raise ValueError("Signing key is not unique")
    selected = jwt.PyJWK.from_dict(matches[0], algorithm=header["alg"]).key
    if header["alg"] == "RS256" and selected.key_size < 2048:
        raise ValueError("Signing key too small")
    claims = jwt.decode(
        raw,
        selected,
        algorithms=[header["alg"]],
        issuer=cfg["issuer"],
        audience=cfg["client_id"],
        leeway=30,
        options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
    )
    if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 500:
        raise ValueError("Invalid subject")
    if not isinstance(claims["nonce"], str) or not hmac.compare_digest(
        claims["nonce"], nonce
    ):
        raise ValueError("Nonce mismatch")
    if claims["iat"] < time.time() - 900:
        raise ValueError("Old ID token")
    audience = claims["aud"]
    if (
        isinstance(audience, list) and len(audience) > 1 or "azp" in claims
    ) and claims.get("azp") != cfg["client_id"]:
        raise ValueError("Authorized party mismatch")
    return claims


@router.get("/api/auth/login")
async def begin_login():
    cfg = settings()
    try:
        discovery = await metadata(cfg)
        state = secrets.token_urlsafe(32)
        browser = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        async with oauth_client(cfg) as client:
            url, _ = client.create_authorization_url(
                discovery["authorization_endpoint"],
                state=state,
                nonce=nonce,
                code_verifier=verifier,
            )
        await asyncio.to_thread(
            store_flow,
            state,
            browser,
            {
                "nonce": nonce,
                "verifier": verifier,
                "issuer": cfg["issuer"],
                "client_id": cfg["client_id"],
                "redirect_uri": cfg["redirect_uri"],
            },
        )
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            "lead_login",
            browser,
            httponly=True,
            secure=public_origin().startswith("https://"),
            samesite="lax",
            max_age=300,
            path="/api/auth",
        )
        return response
    except Exception:
        raise HTTPException(503, "暂时无法连接登录服务") from None


@router.get("/api/auth/callback")
async def callback(request: Request):
    cfg = settings()
    try:
        code = request.query_params.get("code", "")
        if not code or len(code) > 4096:
            raise ValueError("Authorization code missing")
        flow = await asyncio.to_thread(
            consume_flow,
            request.query_params.get("state", ""),
            request.cookies.get("lead_login", ""),
        )
        if any(flow.get(k) != cfg[k] for k in ("issuer", "client_id", "redirect_uri")):
            raise ValueError("Login configuration changed")
        discovery = await metadata(cfg)
        async with oauth_client(cfg) as client:
            tokens = await client.fetch_token(
                discovery["token_endpoint"],
                grant_type="authorization_code",
                code=code,
                code_verifier=flow["verifier"],
            )
        claims = await validate_id_token(
            tokens.get("id_token"), cfg, discovery, flow["nonce"]
        )
        session = await asyncio.to_thread(login, cfg["issuer"], claims["sub"])
        old = request.cookies.get("lead_session", "")
        if old.startswith("lead_s_"):
            await asyncio.to_thread(revoke, old)
        response = RedirectResponse(public_origin() + "/", status_code=302)
        response.delete_cookie("lead_login", path="/api/auth")
        response.set_cookie(
            "lead_session",
            session,
            httponly=True,
            secure=public_origin().startswith("https://"),
            samesite="lax",
            max_age=86400,
        )
        return response
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        logging.getLogger(__name__).warning(
            "OIDC login rejected: %s at %s:%s",
            type(error).__name__,
            frames[-1].name if frames else "",
            frames[-1].lineno if frames else 0,
        )
        raise HTTPException(401, "登录未完成，请重新登录") from None


def revoke(secret):
    with auth_tx() as c:
        c.execute("SELECT lead.auth_logout(%s)", (sha(secret),))


@router.post("/api/auth/logout")
def logout(request: Request):
    if auth_mode() == "oidc":
        revoke(request.cookies.get("lead_session", ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie("lead_session")
    return response


@router.get("/api/auth/me")
def me(request: Request):
    who = principal()
    return {
        "mode": auth_mode(),
        "role": who["role"],
        "tenant_id": who["tenant_id"],
        "subject": who["subject"] if auth_mode() == "oidc" else None,
        "capabilities": who.get("capabilities", []),
        "agent_ids": who.get("agent_ids"),
    }


class MemberChange(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    role: str = Field(pattern="^(owner|operator|viewer)$")
    status: str = Field(default="active", pattern="^(active|revoked)$")


@router.get("/api/workspace/members")
def members(request: Request):
    if auth_mode() != "oidc" or principal()["role"] != "owner":
        raise HTTPException(403)
    with auth_tx() as c:
        return c.execute(
            "SELECT * FROM lead.auth_members(%s)",
            (sha(request.cookies["lead_session"]),),
        ).fetchall()


@router.post("/api/workspace/members")
def change_member(body: MemberChange, request: Request):
    who = principal()
    if auth_mode() != "oidc" or who["role"] != "owner":
        raise HTTPException(403)
    with auth_tx() as c:
        ok = c.execute(
            "SELECT lead.auth_member_change(%s,%s,%s,%s,%s) AS ok",
            (
                sha(request.cookies["lead_session"]),
                who["issuer"],
                body.subject,
                body.role,
                body.status,
            ),
        ).fetchone()["ok"]
    if not ok:
        raise HTTPException(409, "成员权限修改未完成，工作室必须保留至少一名所有者")
    return {"ok": True}


@router.get("/api/workspaces")
def workspaces(request: Request):
    if auth_mode() != "oidc":
        return []
    with auth_tx() as c:
        return c.execute(
            "SELECT * FROM lead.auth_workspaces(%s)",
            (sha(request.cookies["lead_session"]),),
        ).fetchall()


class WorkspaceChange(BaseModel):
    tenant_id: UUID


@router.post("/api/workspace/switch")
def switch_workspace(body: WorkspaceChange, request: Request):
    if auth_mode() != "oidc":
        raise HTTPException(403)
    with auth_tx() as c:
        ok = c.execute(
            "SELECT lead.auth_switch(%s,%s) AS ok",
            (sha(request.cookies["lead_session"]), body.tenant_id),
        ).fetchone()["ok"]
    if not ok:
        raise HTTPException(404, "工作室不存在")
    return {"ok": True}
