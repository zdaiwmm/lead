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

from .config import env
from .db import all_rows, audit, emit, j, one, record, require_agent, tx, uid
from .fetch import resolve_public
from .promotion import CODE_PATTERN, configuration
from .scope import current_tenant
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


class PromotionSetup(BaseModel):
    destination_url: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)


def promotion_settings(c, agent):
    business = one(
        c,
        "SELECT product_url FROM lead.business_profiles ORDER BY revision DESC LIMIT 1",
    )
    return configuration(agent, business)


@router.get("/api/agents/{aid}/analytics/promotion")
def promotion(aid: UUID):
    with tx(str(aid)) as c:
        return promotion_settings(c, require_agent(c, str(aid)))


@router.post("/api/agents/{aid}/analytics/promotion")
def save_promotion(aid: UUID, body: PromotionSetup):
    from .domain import snapshot_preferences

    destination = body.destination_url.strip()
    resolve_public(destination)
    # Normalize existing attribution so pasted promotional links don't stack it.
    p = urlsplit(destination)
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k not in ("lr", "lead_ref")
    ]
    destination = urlunsplit(p._replace(query=urlencode(query)))
    aid = str(aid)
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        old = promotion_settings(c, agent)
        if old["revision"] != body.expected_revision:
            raise HTTPException(409, "推广链接刚有更新，请重新打开配置后保存")
        if old["configured"] and destination == old["destination_url"]:
            return old
        value = {"destination_url": destination, "revision": old["revision"] + 1}
        c.execute(
            "UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{promotion}',%s),updated_at=now() WHERE id=%s",
            (j(value), aid),
        )
        snapshot_preferences(c, aid)
        audit(c, aid, "promotion.updated", {"before": old, "after": value})
        record(c, aid, "已更新推广链接", "后续推广将自动使用新链接并标记社媒来源。")
        emit(c, aid, "agent.updated")
    return {**value, "configured": True}


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
        server = one(
            c,
            "SELECT id,last_received_at FROM lead.analytics_credentials WHERE state='active' ORDER BY created_at DESC LIMIT 1",
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
        "server": server,
    }


@router.get("/api/agents/{aid}/analytics/trend")
def trend(aid: UUID):
    """Actual attributed sessions in the same rolling window as summary metrics."""
    with tx(str(aid)) as c:
        agent = require_agent(c, str(aid))
        from .domain import metrics

        summary = metrics(c)
        channels = all_rows(
            c,
            """
            WITH connected AS (
              SELECT DISTINCT platform FROM lead.social_accounts
              WHERE state='connected' AND platform<>'lab'
            ), traffic AS (
              SELECT l.platform,
                count(*) FILTER(WHERE v.trust_level<>'redirect_observed') AS visits,
                count(*) FILTER(WHERE v.trust_level<>'redirect_observed' AND v.qualified) AS qualified_visits,
                count(*) FILTER(WHERE v.trust_level='redirect_observed' AND v.exclusion_reason IS NULL) AS clicks
              FROM lead.visit_sessions v JOIN lead.tracking_links l ON l.id=v.tracking_link_id
              WHERE v.started_at>now()-interval '30 days' GROUP BY l.platform
            ), posted AS (
              SELECT s.platform,count(*) AS published FROM lead.actions a
              JOIN lead.account_targets t ON t.id=a.target_id
              JOIN lead.social_accounts s ON s.id=t.account_id
              JOIN lead.tasks task ON task.id=a.task_id
              WHERE a.state='confirmed' AND coalesce(task.input->>'bootstrap_verification','false')<>'true'
              GROUP BY s.platform
            ), converted AS (
              SELECT l.platform,count(*) AS conversions FROM lead.conversion_events e
              JOIN lead.visit_sessions v ON v.id=e.visit_session_id
              JOIN lead.tracking_links l ON l.id=v.tracking_link_id
              WHERE e.trust_level='server_verified' AND e.occurred_at>now()-interval '30 days' AND e.type=%s
              GROUP BY l.platform
            )
            SELECT c.platform,coalesce(t.clicks,0) AS clicks,
              coalesce(t.visits,0) AS visits,coalesce(t.qualified_visits,0) AS qualified_visits,
              coalesce(p.published,0) AS published,coalesce(e.conversions,0) AS conversions
            FROM connected c LEFT JOIN traffic t USING(platform)
            LEFT JOIN posted p USING(platform) LEFT JOIN converted e USING(platform)
            ORDER BY c.platform
        """,
            (summary["conversion_objective"],),
        )
        for channel in channels:
            if summary["coverage"] != "connected":
                channel["visits"] = channel["qualified_visits"] = None
            if summary["conversion_coverage"] != "connected":
                channel["conversions"] = None
        promotion_config = promotion_settings(c, agent)
        days = all_rows(
            c,
            """
            WITH daily AS (
              SELECT (started_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
                count(*) FILTER (WHERE trust_level<>'redirect_observed' AND tracking_link_id IS NOT NULL) AS visits,
                count(*) FILTER (WHERE trust_level<>'redirect_observed' AND tracking_link_id IS NOT NULL AND qualified) AS qualified_visits,
                count(*) FILTER (WHERE trust_level='redirect_observed' AND exclusion_reason IS NULL) AS clicks
              FROM lead.visit_sessions WHERE started_at>now()-interval '30 days'
              GROUP BY 1
            )
            SELECT to_char(d.day,'YYYY-MM-DD') AS date,coalesce(v.visits,0) AS visits,
              coalesce(v.qualified_visits,0) AS qualified_visits,coalesce(v.clicks,0) AS clicks
            FROM generate_series((now() AT TIME ZONE 'Asia/Shanghai')::date-30,
              (now() AT TIME ZONE 'Asia/Shanghai')::date,interval '1 day') d(day)
            LEFT JOIN daily v ON v.day=d.day::date ORDER BY d.day
        """,
        )
    return {
        "period": "30d",
        "timezone": "Asia/Shanghai",
        "days": days,
        "channels": channels,
        "promotion": promotion_config,
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
                (current_tenant(), aid, origin),
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
            (
                current_tenant(),
                aid,
                body.platform,
                code,
                body.destination_url,
                p.hostname,
            ),
        )
    return {**row, "url": public_origin() + "/r/" + code}


@router.get("/r/{code}")
def redirect(code: str, request: Request):
    if not re.fullmatch(CODE_PATTERN, code):
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
                current_tenant(),
                link["agent_id"],
                link["id"],
                session,
                "known_bot" if bot else None,
            ),
        )
    query = [
        (k, v)
        for k, v in parse_qsl(dest.query, keep_blank_values=True)
        if k not in ("lr", "lead_ref")
    ]
    query.append(("lead_ref" if len(code) == 16 else "lr", code))
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
        from .admin_controls import attribution, legacy_attribution

        policy, policy_version = attribution(c, aid)
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
                "INSERT INTO lead.visit_sessions(tenant_id,agent_id,tracking_link_id,source,source_session_id,trust_level,qualified,rule_version,started_at,last_seen_at,attribution_policy,attribution_version) VALUES(%s,%s,%s,%s,%s,'client_observed',false,'engaged-v1',now(),now(),%s,%s)",
                (
                    current_tenant(),
                    aid,
                    link["id"] if link else None,
                    "site:" + str(cid),
                    str(body.session_id),
                    j(policy),
                    policy_version,
                ),
            )
        else:
            policy = legacy_attribution(existing["attribution_policy"])
            elapsed = (
                datetime.now(timezone.utc) - existing["started_at"]
            ).total_seconds()
            qualified = existing["qualified"] or (
                body.event == "engagement" and elapsed >= policy["engagement_seconds"]
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
        headers={
            "Access-Control-Allow-Origin": conn["site_host"],
            "Vary": "Origin",
            "X-Lead-Engagement-Seconds": str(policy["engagement_seconds"]),
            "Access-Control-Expose-Headers": "X-Lead-Engagement-Seconds",
        },
    )


@router.get("/lead.js")
def sdk():
    return Response(
        """(()=>{const s=document.currentScript;if(!s)return;const base=new URL(s.src).origin,c=s.dataset.connection,k=s.dataset.key;if(!c||!k)return;let id;try{id=sessionStorage.getItem('lead_sid')||crypto.randomUUID();sessionStorage.setItem('lead_sid',id)}catch{id=crypto.randomUUID()}const q=new URL(location.href).searchParams;let ref=q.get('lr')||q.get('lead_ref');try{if(ref)sessionStorage.setItem('lead_ref',ref);else ref=sessionStorage.getItem('lead_ref')}catch{}const send=event=>fetch(base+'/collect/'+c,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k,session_id:id,event,url:location.href,ref}),keepalive:true,credentials:'omit'}).catch(()=>{});window.leadAttribution=()=>({connection_id:c,session_id:id,ref});send('pageview').then(r=>{const seconds=Math.max(10,Math.min(120,Number(r?.headers.get('X-Lead-Engagement-Seconds'))||10));let active=0,sent=false;const timer=setInterval(()=>{if(!document.hidden&&document.hasFocus())active++;if(active>=seconds&&!sent){sent=true;send('engagement');clearInterval(timer)}},1000)})})();""",
        media_type="application/javascript",
        headers={"Cache-Control": "public,max-age=300"},
    )
