"""Observe only the public redirect. Coverage requests never follow the product URL."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid5

import httpx

from .db import j, one, tx
from .scope import current_tenant


def probe_cookie(aid):
    return uuid5(UUID(str(aid)), "lead-click-coverage-v1").hex


async def sample_redirect(aid):
    from .analytics import public_origin
    from .outbound_links import external_url_problem

    try:
        origin = public_origin()
    except ValueError:
        return
    if external_url_problem(origin, https=True):
        return
    slot = int(datetime.now(timezone.utc).timestamp() // 600)
    with tx(aid) as c:
        link = one(
            c,
            """SELECT l.public_code FROM lead.tasks t JOIN lead.tracking_links l
            ON l.id::text=t.input#>>'{reply,tracking_link_id}'
            WHERE NOT EXISTS(SELECT 1 FROM lead.tasks WHERE workflow_id=%s)
            AND t.type='community.engage' AND t.result->>'reply_state'='confirmed'
            AND l.state='active' ORDER BY t.created_at DESC LIMIT 1""",
            (f"community.coverage:{aid}:{slot}",),
        )
        if not link:
            return
        sample = one(
            c,
            """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at)
            VALUES(%s,%s,'community.coverage','planned',%s,'{}',now())
            ON CONFLICT DO NOTHING RETURNING id""",
            (current_tenant(), aid, f"community.coverage:{aid}:{slot}"),
        )
        if not sample:
            return
    accepted = False
    try:
        async with httpx.AsyncClient(
            timeout=4, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.get(
                f"{origin}/r/{link['public_code']}",
                headers={"User-Agent": "LeadCoverageBot/1"},
                cookies={"lead_click_browser": probe_cookie(aid)},
            )
            accepted = (
                response.status_code == 302
                and response.headers.get("X-Lead-Measurement") == "accepted"
            )
    except httpx.HTTPError:
        pass
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.tasks SET state=%s,result=%s,completed_at=now() WHERE id=%s",
            (
                "succeeded" if accepted else "failed",
                j({"accepted": accepted}),
                sample["id"],
            ),
        )


def rank_candidates(c, pool):
    """Use comparable 24-hour clicks for exploitation; regularly serve under-tested scenes."""
    from .community_growth import audience_lane

    rows = c.execute("""WITH actions AS (
            SELECT input#>>'{candidate,audience_lane}' AS lane
            FROM lead.tasks WHERE type='community.engage' AND NOT input ? 'membership_task_id'
              AND input#>>'{candidate,audience_lane}' IS NOT NULL AND created_at>now()-interval '7 days'
        ), clicks AS (
            SELECT input#>>'{candidate,audience_lane}' AS lane,
                   avg((result#>>'{click_windows,24h,clicks}')::int) AS clicks_per_post, count(*) AS measured_posts
            FROM lead.tasks WHERE type='community.engage' AND result->>'reply_state'='confirmed'
              AND input#>>'{reply,promote}'='true'
              AND result#>>'{click_windows,24h,sampled_coverage}'='true'
              AND (result#>>'{click_windows,24h,until}')::timestamptz>now()-interval '7 days'
            GROUP BY input#>>'{candidate,audience_lane}')
        SELECT a.lane,count(*) AS tried,max(k.clicks_per_post) AS yield, coalesce(max(k.measured_posts),0) AS measured_posts
        FROM actions a LEFT JOIN clicks k ON k.lane=a.lane GROUP BY a.lane""").fetchall()
    stats = {row["lane"]: row for row in rows}
    costs = c.execute("""SELECT t.input#>>'{candidate,audience_lane}' AS lane,
        avg(coalesce(timing.seconds,0)) AS seconds,avg(coalesce(tokens.units,0)) AS tokens,
        count(*) FILTER (WHERE t.result->>'reply_state'='failed') AS failures
        FROM lead.tasks t LEFT JOIN LATERAL (
          SELECT sum((step->>'duration_ms')::float)/1000 AS seconds
          FROM jsonb_array_elements(coalesce(t.result->'execution_steps','[]')) step
        ) timing ON true LEFT JOIN LATERAL (
          SELECT sum(e.units) AS units FROM lead.budget_reservations r JOIN lead.usage_events e ON e.reservation_id=r.id
          WHERE r.pricing_snapshot#>>'{trial,action}'=t.id::text AND e.category='model_tokens'
        ) tokens ON true
        WHERE t.type='community.engage' AND t.created_at>now()-interval '7 days'
        GROUP BY t.input#>>'{candidate,audience_lane}'""").fetchall()
    costs = {row["lane"]: row for row in costs}
    explore = sum(row["tried"] for row in rows) % 3 == 0

    def order(item):
        lane = item.get("audience_lane") or audience_lane(item.get("name", ""))
        data = stats.get(lane, {})
        cost = costs.get(lane, {})
        return (
            0 if explore else -(data.get("yield") or 0),
            0 if explore or data.get("yield") is None else 1 if data.get("yield") == 0 and data.get("measured_posts", 0) >= 3 else 0,
            data.get("tried", 0),
            cost.get("failures", 0),
            float(cost.get("seconds") or 0) / 60 + float(cost.get("tokens") or 0) / 1000,
            -(item.get("online") or 0),
            -(item.get("members") or 0),
        )

    return sorted(pool, key=order)


def refresh_observations(c, aid, agent, business, now):
    """Catch up overdue windows after a restart. No visits to destination pages."""
    rows = c.execute("""SELECT t.id,t.input#>>'{reply,tracking_link_id}' AS link,l.state AS link_state,
        coalesce((t.result->>'reply_dispatched_at')::timestamptz,t.completed_at) AS sent,
        t.result->'click_windows' AS windows
        FROM lead.tasks t JOIN lead.tracking_links l ON l.id::text=t.input#>>'{reply,tracking_link_id}'
        WHERE t.type='community.engage' AND t.result->>'reply_state'='confirmed'
        AND t.input#>>'{reply,promote}'='true'
        AND coalesce((t.result->>'reply_dispatched_at')::timestamptz,t.completed_at)<=now()-interval '1 hour'
        AND (NOT coalesce(t.result->'click_windows','{}') ? '1h' OR (t.completed_at>now()-interval '7 days' AND (t.result->>'windows_checked_at')::timestamptz<now()-interval '1 hour') OR
          (coalesce((t.result->>'reply_dispatched_at')::timestamptz,t.completed_at)<=now()-interval '24 hours'
          AND NOT coalesce(t.result->'click_windows','{}') ? '24h'))
        ORDER BY t.created_at LIMIT 12""").fetchall()

    for row in rows:
        windows = dict(row["windows"] or {})
        for hours in (1, 24):
            key = f"{hours}h"
            stop = row["sent"] + timedelta(hours=hours)
            if stop > now:
                continue
            observed = one(
                c,
                """SELECT count(*) AS clicks FROM lead.visit_sessions
                WHERE tracking_link_id=%s AND trust_level='redirect_observed' AND exclusion_reason IS NULL
                  AND started_at>=%s AND started_at<%s""",
                (row["link"], row["sent"], stop),
            )
            coverage = one(
                c,
                """SELECT NOT EXISTS(SELECT 1 FROM generate_series(
                floor(extract(epoch FROM %s::timestamptz)/900)::bigint,
                floor(extract(epoch FROM %s::timestamptz)/900)::bigint) slots(n)
                WHERE NOT EXISTS(SELECT 1 FROM lead.visit_sessions v WHERE v.exclusion_reason='coverage_probe'
                  AND v.started_at>=to_timestamp(n*900) AND v.started_at<to_timestamp((n+1)*900))) AS sampled""",
                (row["sent"], stop),
            )
            healthy = coverage["sampled"] and row["link_state"] == "active"
            windows[key] = {
                "clicks": observed["clicks"] if observed["clicks"] or healthy else None,
                "sampled_coverage": healthy,
                "from": row["sent"].isoformat(),
                "until": stop.isoformat(),
            }
        c.execute(
            "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
            (
                j({"click_windows": windows, "windows_checked_at": now.isoformat()}),
                row["id"],
            ),
        )

    prior = agent["onboarding_state"].get("outreach_hour") or {}
    if prior.get("checked") and now - datetime.fromisoformat(
        prior["checked"]
    ) < timedelta(hours=1):
        return
    summary = one(
        c,
        """SELECT count(*) FILTER(WHERE type='community.engage' AND result->>'reply_state'='confirmed') AS touches,
        count(*) FILTER(WHERE type='community.engage' AND result->>'reply_state'='confirmed'
            AND input#>>'{reply,promote}'='true' AND input#>>'{reply,tracking_link_id}' IS NOT NULL) AS placements,
        count(*) FILTER(WHERE type='community.discover' AND state='succeeded') AS batches,
        coalesce(sum(jsonb_array_length(coalesce(result->'candidates','[]'))) FILTER(WHERE type='community.discover'),0) AS candidates
        FROM lead.tasks WHERE coalesce(completed_at,created_at)>=%s""",
        (now - timedelta(hours=1),),
    )
    from .operating_feedback import enqueue

    exhausted_search = summary["batches"] >= 2 and not summary["candidates"]
    conversation_only = summary["touches"] >= 2 and not summary["placements"]
    if exhausted_search or conversation_only:
        summary["next_action"] = "切换公开讨论或目录来源，优先核验原帖和参与入口"
        enqueue(
            c,
            aid,
            agent,
            business,
            "opportunities",
            summary["next_action"],
            work_key="alternatives:public-discovery",
        )
    else:
        summary["next_action"] = "处理可触达机会和到期回访，继续补充新场景"
    summary["checked"] = now.isoformat()
    c.execute(
        "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
        (j({"outreach_hour": summary}), aid),
    )
