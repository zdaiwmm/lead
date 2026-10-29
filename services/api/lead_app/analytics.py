"""First-party measurement. Redirects are clicks; visits require site events.
Client-observed engagement is never represented as server-verified revenue.
"""

import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field

from .config import TENANT, env
from .db import all_rows, audit, one, require_agent, tx, uid
from .fetch import resolve_public
from .security import token, valid

router = APIRouter()


def public_origin():
    value = (env().get("LEAD_PUBLIC_URL") or "http://127.0.0.1:3100").rstrip("/")
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("公开地址无效")
    return value


class AnalyticsSetup(BaseModel):
    site_url: str = Field(max_length=2000)


class TrackingSetup(BaseModel):
    destination_url: str = Field(max_length=2000)
    platform: str = Field(default="direct", pattern=r"^[a-z_]{1,30}$")


class Event(BaseModel):
    key: str = Field(max_length=500)
    session_id: UUID
    event: str = Field(pattern=r"^(pageview|engagement)$")
    url: str = Field(max_length=3000)
    ref: str | None = Field(default=None, max_length=100)


@router.get("/api/agents/{aid}/analytics")
def settings(aid: UUID):
    aid = str(aid)
    with tx(aid) as c:
        require_agent(c, aid)
        conn = one(
            c,
            "SELECT id,site_host,state,last_received_at FROM lead.analytics_connections WHERE provider='lead_site' ORDER BY created_at DESC LIMIT 1",
        )
        links = all_rows(
            c,
            "SELECT id,public_code,platform,destination_url FROM lead.tracking_links ORDER BY created_at DESC",
        )
    if conn:
        cid = str(conn["id"])
        write_key = token("analytics:" + cid, 365 * 86400)
        conn["snippet"] = (
            f'<script defer src="{public_origin()}/lead.js" data-connection="{cid}" data-key="{write_key}"></script>'
        )
    return {
        "connection": conn,
        "links": [
            {**x, "url": public_origin() + "/r/" + x["public_code"]} for x in links
        ],
        "public_url": public_origin(),
    }


@router.post("/api/agents/{aid}/analytics")
def setup(aid: UUID, body: AnalyticsSetup):
    resolve_public(body.site_url)
    aid = str(aid)
    p = urlsplit(body.site_url)
    origin = p.scheme + "://" + p.netloc
    with tx(aid) as c:
        require_agent(c, aid, True)
        conn = one(
            c,
            "SELECT id FROM lead.analytics_connections WHERE provider='lead_site' AND site_host=%s",
            (origin,),
        )
        if not conn:
            c.execute(
                "INSERT INTO lead.analytics_connections(tenant_id,agent_id,provider,site_host,secret_ref,state) VALUES(%s,%s,'lead_site',%s,'hmac:client-write','pending')",
                (TENANT, aid, origin),
            )
        audit(c, aid, "analytics.setup", {"site_host": origin})
    return settings(UUID(aid))


@router.post("/api/agents/{aid}/tracking-links")
def create_link(aid: UUID, body: TrackingSetup):
    resolve_public(body.destination_url)
    aid = str(aid)
    p = urlsplit(body.destination_url)
    with tx(aid) as c:
        require_agent(c, aid)
        code = __import__("secrets").token_urlsafe(12)
        row = one(
            c,
            "INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id,public_code",
            (TENANT, aid, body.platform, code, body.destination_url, p.hostname),
        )
    return {**row, "url": public_origin() + "/r/" + code}


@router.get("/r/{code}")
def redirect(code: str, request: Request):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16}", code):
        raise HTTPException(404)
    with tx() as c:
        link = one(
            c,
            "SELECT * FROM lead.tracking_links WHERE public_code=%s AND state='active'",
            (code,),
        )
    if not link:
        raise HTTPException(404)
    dest = urlsplit(link["destination_url"])
    if dest.hostname != link["allowed_host"] or dest.scheme not in ("http", "https"):
        raise HTTPException(409)
    session = uid()
    bot = bool(
        re.search(
            r"bot|crawler|spider|preview|slack|facebookexternalhit",
            request.headers.get("user-agent", ""),
            re.I,
        )
    )
    with tx(str(link["agent_id"])) as c:
        c.execute(
            "INSERT INTO lead.visit_sessions(tenant_id,agent_id,tracking_link_id,source,source_session_id,trust_level,qualified,exclusion_reason,rule_version,started_at,last_seen_at) VALUES(%s,%s,%s,'redirect',%s,'redirect_observed',NULL,%s,'engaged-v1',now(),now())",
            (
                TENANT,
                link["agent_id"],
                link["id"],
                session,
                "known_bot" if bot else None,
            ),
        )
    query = dict(parse_qsl(dest.query))
    query["lead_ref"] = code
    return RedirectResponse(
        urlunsplit(dest._replace(query=urlencode(query))), status_code=302
    )


@router.options("/collect/{cid}")
def preflight(cid: UUID, request: Request):
    with tx() as c:
        conn = one(
            c, "SELECT site_host FROM lead.analytics_connections WHERE id=%s", (cid,)
        )
    if not conn or request.headers.get("origin") != conn["site_host"]:
        raise HTTPException(403)
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": conn["site_host"],
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Vary": "Origin",
        },
    )


@router.post("/collect/{cid}")
def collect(cid: UUID, body: Event, request: Request):
    with tx() as c:
        conn = one(
            c,
            "SELECT * FROM lead.analytics_connections WHERE id=%s AND state<>'disconnected'",
            (cid,),
        )
    if (
        not conn
        or not valid(body.key, "analytics:" + str(cid))
        or request.headers.get("origin") != conn["site_host"]
    ):
        raise HTTPException(403)
    p = urlsplit(body.url)
    if p.scheme + "://" + p.netloc != conn["site_host"]:
        raise HTTPException(403)
    if re.search(
        r"bot|crawler|spider|headless", request.headers.get("user-agent", ""), re.I
    ):
        return Response(
            status_code=204, headers={"Access-Control-Allow-Origin": conn["site_host"]}
        )
    aid = str(conn["agent_id"])
    with tx(aid) as c:
        link = (
            one(
                c,
                "SELECT id FROM lead.tracking_links WHERE public_code=%s AND state='active'",
                (body.ref,),
            )
            if body.ref
            else None
        )
        existing = one(
            c,
            "SELECT * FROM lead.visit_sessions WHERE source=%s AND source_session_id=%s FOR UPDATE",
            ("site:" + str(cid), str(body.session_id)),
        )
        if not existing:
            if body.event != "pageview":
                return Response(
                    status_code=204,
                    headers={"Access-Control-Allow-Origin": conn["site_host"]},
                )
            c.execute(
                "INSERT INTO lead.visit_sessions(tenant_id,agent_id,tracking_link_id,source,source_session_id,trust_level,qualified,rule_version,started_at,last_seen_at) VALUES(%s,%s,%s,%s,%s,'client_observed',false,'engaged-v1',now(),now())",
                (
                    TENANT,
                    aid,
                    link["id"] if link else None,
                    "site:" + str(cid),
                    str(body.session_id),
                ),
            )
        else:
            elapsed = (
                datetime.now(timezone.utc) - existing["started_at"]
            ).total_seconds()
            qualified = existing["qualified"] or (
                body.event == "engagement" and elapsed >= 10
            )
            c.execute(
                "UPDATE lead.visit_sessions SET last_seen_at=now(),qualified=%s WHERE id=%s",
                (qualified, existing["id"]),
            )
        c.execute(
            "UPDATE lead.analytics_connections SET state='verified',last_received_at=now() WHERE id=%s",
            (cid,),
        )
    return Response(
        status_code=204,
        headers={"Access-Control-Allow-Origin": conn["site_host"], "Vary": "Origin"},
    )


@router.get("/lead.js")
def sdk():
    return Response(
        """(()=>{const s=document.currentScript;if(!s)return;const base=new URL(s.src).origin,c=s.dataset.connection,k=s.dataset.key;if(!c||!k)return;let id;try{id=sessionStorage.getItem('lead_sid')||crypto.randomUUID();sessionStorage.setItem('lead_sid',id)}catch{id=crypto.randomUUID()}let ref=new URL(location.href).searchParams.get('lead_ref');try{if(ref)sessionStorage.setItem('lead_ref',ref);else ref=sessionStorage.getItem('lead_ref')}catch{}const send=event=>fetch(base+'/collect/'+c,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k,session_id:id,event,url:location.href,ref}),keepalive:true,credentials:'omit'}).catch(()=>{});send('pageview');let active=0,sent=false;const timer=setInterval(()=>{if(!document.hidden&&document.hasFocus())active++;if(active>=10&&!sent){sent=true;send('engagement');clearInterval(timer)}},1000)})();""",
        media_type="application/javascript",
        headers={"Cache-Control": "public,max-age=300"},
    )
