"""Read-only presentation of plan work and its original execution records.

No schedule, plan version, task, publication or collector is changed here.
"""

from datetime import date, datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from .db import all_rows, one, require_agent, tx

router = APIRouter()


def enrich(c, aid, plan, until=None):
    items = plan["day"]["queue"]
    ids = list({item["evidence_task_id"] for item in items})
    if (
        until is None
        and plan.get("day", {}).get("current", {}).get("state") == "waiting"
    ):
        # A planner can be actively preparing a channel before a business task exists.
        decision = one(
            c,
            """SELECT state, result->'selected'->>'kind' AS kind,
            started_at > now()-interval '15 minutes' AS recent
            FROM lead.tasks WHERE agent_id=%s AND type='operating.decision'
            AND input->>'config_revision'=%s ORDER BY created_at DESC,id DESC LIMIT 1""",
            (aid, str(plan.get("config_revision"))),
        )
        if (
            decision
            and decision["state"] == "planned"
            and decision["kind"]
            and decision["recent"]
        ):
            plan["day"]["current"].update(
                state="running",
                title={
                    "account": "核对渠道账号",
                    "kits": "准备渠道资料",
                    "publish": "规划渠道发布",
                    "content": "准备渠道稿件",
                    "publication_reconcile": "核对发布回执",
                    "community_discovery": "查找可用社区",
                    "community_progress": "推进社区任务",
                }.get(decision["kind"], "处理本轮运营任务"),
            )
        current = plan["day"]["current"]
        if current["state"] == "waiting":
            other = one(
                c,
                """SELECT type FROM lead.tasks WHERE agent_id=%s AND state='running'
                AND type<>'operating.decision' ORDER BY started_at DESC NULLS LAST LIMIT 1""",
                (aid,),
            )
            browser = (
                None
                if other
                else one(
                    c,
                    """SELECT mode FROM lead.browser_runs
                WHERE agent_id=%s AND state='running' AND ended_at IS NULL
                ORDER BY started_at DESC LIMIT 1""",
                    (aid,),
                )
            )
            if other or browser:
                current.update(
                    state="running",
                    title={
                        "account.prepare": "准备渠道账号",
                        "account.check_login": "核对账号登录状态",
                    }.get(other["type"], "处理后台运营任务")
                    if other
                    else "浏览并核对网页资料",
                )
    if not ids:
        return plan
    rows = all_rows(
        c,
        """SELECT t.id,t.created_at,t.started_at,t.type,t.input->>'topic' AS topic, parent.input->>'topic' AS parent_topic,
        parent.id AS parent_id,parent.type AS parent_type,parent.completed_at AS parent_at,
        coalesce(parent.result->>'title',parent.input->>'title',parent.input->>'topic') AS parent_title,
        parent.input->>'platform' AS parent_platform, manuscript.created_at AS manuscript_at, manuscript.source_refs AS publication_refs,
        observation.clicks,observation.last_at,
        coalesce(t.input->>'platform',s.platform,setup.platform) AS platform,
        recent.title AS latest_title,recent.created_at AS latest_at,recent.level AS latest_level
        FROM lead.tasks t
        LEFT JOIN lead.content_items manuscript ON t.type LIKE '%%.publish'
          AND manuscript.agent_id=t.agent_id AND manuscript.id::text=t.input->>'content_id'
        LEFT JOIN LATERAL (SELECT count(*) AS clicks,max(v.started_at) AS last_at
          FROM lead.tracking_links l JOIN lead.visit_sessions v ON v.tracking_link_id=l.id
          WHERE l.content_id=manuscript.id AND l.agent_id=t.agent_id AND v.agent_id=t.agent_id
          AND v.trust_level='redirect_observed' AND v.exclusion_reason IS NULL
          AND (%s::timestamptz IS NULL OR v.started_at<=%s)) observation ON true
        LEFT JOIN lead.tasks parent ON parent.agent_id=t.agent_id AND parent.state='succeeded'
          AND parent.id::text=CASE WHEN t.type LIKE '%%.publish' THEN t.input->>'prepared_content_id'
              WHEN t.type='community.engage' THEN t.input->'candidate'->>'discovery_task_id'
              WHEN t.type='operating.strategy' THEN t.input->>'required_evidence_task_id' END
          AND (%s::timestamptz IS NULL OR parent.completed_at<=%s)
        LEFT JOIN lead.account_targets target ON target.id::text=t.input->>'target_id'
        LEFT JOIN lead.social_accounts s ON s.id=target.account_id
        LEFT JOIN lead.social_accounts setup ON setup.id::text=t.input->>'account_id'
        LEFT JOIN LATERAL(SELECT title,created_at,level FROM lead.activity
          WHERE task_id=t.id AND agent_id=%s AND (%s::timestamptz IS NULL OR created_at<=%s) ORDER BY created_at DESC,id DESC LIMIT 1) recent ON true
        WHERE t.id=ANY(%s::uuid[]) AND t.agent_id=%s""",
        (until, until, until, until, aid, until, until, ids, aid),
    )
    by_id = {str(row["id"]): row for row in rows}
    for item in items:
        source = by_id.get(item["evidence_task_id"])
        if not source:
            continue
        synthetic = item["id"].startswith("distribute:")
        item.update(
            {
                "synthetic": synthetic,
                "activity_task_id": None if synthetic else item["evidence_task_id"],
                "source_task_id": item["evidence_task_id"] if synthetic else None,
                "created_at": None if synthetic else source["created_at"].isoformat(),
                "started_at": source["started_at"].isoformat()
                if not synthetic and source["started_at"]
                else None,
                "latest_activity": {
                    "title": source["latest_title"],
                    "at": source["latest_at"].isoformat(),
                    "level": source["latest_level"],
                }
                if not synthetic and source["latest_at"]
                else None,
            }
        )
        if item["stage"] in ("content", "distribution"):
            topic = (
                source["topic"]
                if source["type"] == "content.prepare"
                else source["parent_topic"]
            )
            if topic:
                item["basis_task_id"] = str(
                    source["id"]
                    if source["type"] == "content.prepare"
                    else source["parent_id"]
                )
            elif source["manuscript_at"] and source["publication_refs"]:
                item["basis_task_id"] = str(source["id"])
            action = "准备" if item["stage"] == "content" else "发布"
            subject = {
                "新读者入门索引": "给新读者的入门索引",
                "一个常见问题的处理步骤": "常见问题的分步解答",
            }.get(topic, topic)
            item["task_title"] = action + (subject or "渠道内容")
        else:
            item["task_title"] = {
                "community.discover": "寻找 Telegram 讨论群并核对群规",
                "community.engage": item["title"],
                "operating.validate": "核对新线索是否值得推进",
                "operating.opportunities": "寻找目标受众的需求线索",
                "operating.journey": "核对用户进入产品的路径",
                "operating.strategy": "根据已有证据评估下一步运营方案",
            }.get(
                source["type"],
                "复盘运营效果" if item["stage"] == "observation" else "研究受众需求",
            )
        item["platform"] = item.get("platform") or source["platform"]
        if (
            item["stage"] == "distribution"
            and not synthetic
            and source["manuscript_at"]
        ):
            item["manuscript_at"] = source["manuscript_at"].isoformat()
            if source["clicks"] and item.get("receipt") and item["state"] == "done":
                item["observed_clicks"] = source["clicks"]
                item["observed_at"] = source["last_at"].isoformat()
                item["observed_today"] = (
                    source["last_at"]
                    .astimezone(ZoneInfo(plan["timezone"]))
                    .date()
                    .isoformat()
                    == plan["day"]["period"]
                )
        if source["parent_id"] and source["parent_at"] and not synthetic:
            from .operating_plans import LABELS, stage_for

            stage = stage_for(source["parent_type"])
            item["parent"] = {
                "id": str(source["parent_id"]),
                "evidence_task_id": str(source["parent_id"]),
                "stage": stage,
                "title": source["parent_title"] or LABELS[stage],
                "state": "done",
                "completed_at": source["parent_at"].isoformat(),
                "platform": source["parent_platform"],
            }
    return plan


SNAPSHOTS = """FROM lead.memory_entries WHERE agent_id=%s AND kind='observation'
    AND body->>'type'='operating_plan' AND body->>'horizon'='day' AND deleted_at IS NULL"""
BUSINESS_ACTIVITY = """FROM lead.activity a JOIN lead.tasks t ON t.id=a.task_id AND t.agent_id=a.agent_id
    WHERE a.agent_id=%s AND (t.type IN ('content.prepare','community.discover','community.engage','operating.journey','operating.opportunities',
    'operating.validate','operating.strategy') OR t.type LIKE '%%.publish')
    AND (coalesce(t.input->>'bootstrap_verification','false')<>'true' OR t.input->>'member_contribution'='true')"""


def history_zone(c, agent):
    from .operating_plans import periods

    business = one(
        c, "SELECT facts FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
    )
    return periods(agent, business or {}, datetime.now(timezone.utc))[0]


@router.get("/api/agents/{aid}/plans/history")
def history_dates(aid: UUID, month: date):
    start = month.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    with tx(str(aid)) as c:
        agent = require_agent(c, str(aid))
        zone = history_zone(c, agent)
        rows = all_rows(
            c,
            "SELECT body->>'period' AS day "
            + SNAPSHOTS
            + " AND body->>'period'>=%s AND body->>'period'<%s UNION SELECT DISTINCT "
            "to_char(a.created_at AT TIME ZONE %s,'YYYY-MM-DD') AS day "
            + BUSINESS_ACTIVITY
            + " AND a.created_at >= (%s::date::timestamp AT TIME ZONE %s) "
            "AND a.created_at < (%s::date::timestamp AT TIME ZONE %s)",
            (
                aid,
                start.isoformat(),
                end.isoformat(),
                zone,
                aid,
                start,
                zone,
                end,
                zone,
            ),
        )
        return {"dates": sorted(row["day"] for row in rows)}


@router.get("/api/agents/{aid}/plans/history/{day}")
def history_day(aid: UUID, day: date, offset: int = Query(default=0, ge=0)):
    with tx(str(aid)) as c:
        agent = require_agent(c, str(aid))
        saved = one(
            c,
            "SELECT body "
            + SNAPSHOTS
            + " AND body->>'period'=%s ORDER BY created_at DESC LIMIT 1",
            (aid, day.isoformat()),
        )
        snapshot = saved["body"] if saved else {}
        zone = snapshot.get("timezone") or history_zone(c, agent)
        start = datetime.combine(day, datetime.min.time(), ZoneInfo(zone))
        end = start + timedelta(days=1)
        records = all_rows(
            c,
            "SELECT a.id,a.title,a.detail,a.created_at,a.task_id,t.type, "
            "coalesce(t.input->>'platform',CASE WHEN t.type LIKE '%%.publish' THEN split_part(t.type,'.',1) END) AS platform "
            + BUSINESS_ACTIVITY
            + " AND a.created_at>=%s AND a.created_at<%s ORDER BY a.created_at DESC,a.id DESC LIMIT 51 OFFSET %s",
            (aid, start, end, offset),
        )
        from .operating_plans import stage_for

        for entry in records:
            entry["stage"] = stage_for(entry.pop("type"))
        plan = {
            "day": snapshot.get("plan", {"period": day.isoformat(), "queue": []}),
            "timezone": zone,
            "updated_at": snapshot.get("updated_at"),
            "records": records[:50],
            "has_more": len(records) > 50,
        }
        return (
            enrich(c, str(aid), plan, snapshot.get("updated_at"))
            if offset == 0
            else {"records": records[:50], "has_more": len(records) > 50}
        )


@router.get("/api/agents/{aid}/plans/work/{task_id}/basis")
def topic_basis(aid: UUID, task_id: UUID, until: datetime | None = None):
    from .admin_content import telegram_content
    from .operating_playbooks import channel_for

    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        row = one(
            c,
            """SELECT t.input->>'topic' topic,t.input->>'plan_id' plan_id,t.type,
            input->>'platform' platform,input->>'strategy_version' version,
            CASE WHEN %s::timestamptz IS NULL OR t.completed_at<=%s THEN t.result->'fact_quotes' END quotes,
            CASE WHEN t.type='content.prepare' THEN bp.source_refs ELSE ci.source_refs END refs,
            bp.revision AS business_revision
            FROM lead.tasks t LEFT JOIN lead.content_items ci ON ci.agent_id=t.agent_id AND ci.id::text=t.input->>'content_id'
            LEFT JOIN lead.business_profiles bp ON bp.agent_id=t.agent_id AND bp.id::text=t.input->>'business_profile_id'
            WHERE t.agent_id=%s AND t.id=%s AND (t.type='content.prepare' OR t.type LIKE '%%.publish')
            AND (%s::timestamptz IS NULL OR t.created_at<=%s)""",
            (until, until, aid, task_id, until, until),
        )
        if not row:
            raise HTTPException(404, "未找到这篇内容的选题记录")
        if row["version"]:
            key, _, version = row["version"].rpartition("@")
            archived = (
                one(
                    c,
                    """SELECT after_state->'body' AS body FROM lead.admin_config_versions
                WHERE config_key=%s AND version=%s AND action IN ('publish','rollback') LIMIT 1""",
                    (key, int(version)),
                )
                if version.isdecimal()
                else None
            )
            strategy = archived["body"] if archived else {}
        else:
            strategy = (
                telegram_content()
                if row["platform"] == "telegram"
                else channel_for(row["platform"])
            )
        plan = next(
            (p for p in (strategy or {}).get("plans", []) if p["id"] == row["plan_id"]),
            {},
        )
        refs = [ref for ref in row["refs"] or [] if isinstance(ref, dict)]
        sources = [
            {"title": ref.get("title") or ref["url"], "url": ref["url"]}
            for ref in refs
            if isinstance(ref.get("url"), str)
            and ref["url"].startswith(("https://", "http://"))
        ]
        return {
            "sources": sources,
            "business": bool(
                row["business_revision"]
                or any(ref.get("business_profile_id") for ref in refs)
            ),
            "topic": row["topic"],
            "plan": plan.get("name") or row["plan_id"],
            "purpose": plan.get("hypothesis"),
            "quotes": row["quotes"] or [],
        }


@router.get("/api/agents/{aid}/plans/work/{task_id}/activity")
def work_activity(
    aid: UUID,
    task_id: UUID,
    offset: int = Query(default=0, ge=0),
    until: datetime | None = None,
    observation: bool = False,
):
    aid, task_id = str(aid), str(task_id)
    with tx(aid) as c:
        require_agent(c, aid)
        task = one(
            c,
            """SELECT t.type,t.result->'query_plan' AS searches,t.result->'candidates' AS candidates,t.input->>'content_id' AS content_id,t.completed_at,t.result->>'summary' AS summary,t.result->'findings' AS findings,
            t.result->'alternatives' AS alternatives,t.result->>'bottleneck' AS bottleneck,
            t.result->'fact_quotes' AS fact_quotes,t.result->>'limitations' AS limitations,t.result->'source_urls' AS source_urls,t.input->>'format' AS format,
            CASE WHEN t.type='content.prepare' THEN t.result->>'body'
                 WHEN t.type LIKE '%%.publish' THEN coalesce(ci.body->>'body',t.input->>'body') END AS body,
            CASE WHEN t.type='content.prepare' THEN t.result->'asset_ids'
                 WHEN t.type LIKE '%%.publish' THEN coalesce(ci.body->'media',t.input->'media') END AS media
            FROM lead.tasks t LEFT JOIN lead.content_items ci
              ON ci.agent_id=t.agent_id AND ci.id::text=t.input->>'content_id'
            WHERE t.id=%s AND t.agent_id=%s""",
            (task_id, aid),
        )
        if not task:
            raise HTTPException(404, "这项工作不存在或不可查看")
        if observation:
            rows = all_rows(
                c,
                """SELECT v.id,v.started_at AS created_at,'有效推广点击' AS title
                FROM lead.tracking_links l JOIN lead.visit_sessions v ON v.tracking_link_id=l.id
                WHERE l.agent_id=%s AND v.agent_id=l.agent_id AND l.content_id::text=%s
                AND v.trust_level='redirect_observed' AND v.exclusion_reason IS NULL
                AND (%s::timestamptz IS NULL OR v.started_at<=%s)
                ORDER BY v.started_at DESC,v.id DESC LIMIT 51 OFFSET %s""",
                (aid, task["content_id"], until, until, offset),
            )
            return {"rows": rows[:50], "has_more": len(rows) > 50}
        rows = all_rows(
            c,
            """SELECT a.id,a.title,a.detail,a.level,a.created_at,a.task_id,
            coalesce(t.input->>'platform',s.platform,setup.platform) AS platform,
            t.state AS task_state
            FROM lead.activity a JOIN lead.tasks t ON t.id=a.task_id
            LEFT JOIN lead.account_targets target ON target.id::text=t.input->>'target_id'
            LEFT JOIN lead.social_accounts s ON s.id=target.account_id
            LEFT JOIN lead.social_accounts setup ON setup.id::text=t.input->>'account_id'
            WHERE a.task_id=%s AND a.agent_id=%s AND (%s::timestamptz IS NULL OR a.created_at<=%s)
            ORDER BY a.created_at DESC,a.id DESC LIMIT 51 OFFSET %s""",
            (task_id, aid, until, until, offset),
        )
        response = {"rows": rows[:50], "has_more": len(rows) > 50}
        if until and until.tzinfo is None:
            raise HTTPException(422, "时间必须包含时区")
        if (
            until
            and not task["type"].endswith(".publish")
            and (not task["completed_at"] or task["completed_at"] > until)
        ):
            return response
        if offset == 0 and task["type"] == "community.discover":
            plan=task["searches"] or {}
            lines=[plan.get("rationale"),*plan.get("web_queries",[])]
            for group in task["candidates"] or []:
                sources = dict.fromkeys(filter(None, (
                    group.get("telegram_url"), group.get("source_url"), group.get("rules_source_url")
                )))
                quote = group.get("rules_quote")
                lines.extend([group.get("name"), group.get("reading_need"), group.get("rules_summary"),
                              f"群规原文 · {quote}" if quote else None, *sources])
            if task["summary"]:
                lines.append(task["summary"])
            if any(lines):
                response["material"]={"format":"report","assets":[],"body":"\n\n".join(t for t in lines if t)}
        if offset == 0 and task["type"].startswith("operating."):
            parts = [task["summary"], task["bottleneck"]]
            for finding in task["findings"] or []:
                parts.extend(
                    finding.get(key)
                    for key in ("observation", "quote", "source_url", "next_step")
                )
            for option in task["alternatives"] or []:
                parts.extend(
                    option.get(key)
                    for key in ("name", "mechanism", "next_action", "validation")
                )
            parts.extend(task["source_urls"] or [])
            parts.append(task["limitations"])
            response["material"] = {
                "body": "\n\n".join(
                    part for part in parts if isinstance(part, str) and part
                ),
                "format": "report",
                "assets": [],
            }
        if offset == 0 and (
            task["type"] == "content.prepare" or task["type"].endswith(".publish")
        ):
            ids = [
                m.get("id") if isinstance(m, dict) else m for m in task["media"] or []
            ]
            assets = (
                all_rows(
                    c,
                    """SELECT id::text, media_type FROM lead.assets
                WHERE agent_id=%s AND state='ready' AND id::text=ANY(%s)""",
                    (aid, ids),
                )
                if ids
                else []
            )
            available = {a["id"]: a for a in assets}
            response["material"] = {
                "body": task["body"],
                "fact_quotes": task["fact_quotes"] or [],
                "format": task["format"],
                "assets": [available.get(i) for i in ids],
            }
        return response
