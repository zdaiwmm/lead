"""Product-neutral, durable plans driven by execution evidence, never model polling.

The acquisition adapter below only reads the collector's existing records. Link
creation, redirects, filtering and event ingestion belong to the analytics owner.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import acquisition_strategy
from .db import all_rows, audit, emit, j, one, require_agent, tx, uid
from .operating_contract import configuration
from .operating_plan_views import enrich as enrich_view
from .operating_plan_views import router as views_router
from .scope import current_tenant

router = APIRouter()
router.include_router(views_router)
STAGES = ["audience", "content", "distribution", "observation"]
LABELS = {
    "audience": "选择受众与验证方向",
    "content": "准备具体内容",
    "distribution": "按渠道节奏分发",
    "observation": "观察效果并调整",
}
EVENT_NAMES = {
    "signup": "CompleteRegistration",
    "lead": "Lead",
    "trial": "StartTrial",
    "purchase": "Purchase",
}
PLATFORM_KINDS = {"account", "kits", "publication_reconcile", "operating.measurement"}
REASONS = {
    "scheduled": "等待原定发布间隔",
    "channel_calendar": "等待渠道发布窗口",
    "token_budget_wait": "等待模型额度恢复，已有内容仍可分发",
    "model_rate_limit_cooldown": "模型服务限流，按退避时间恢复",
    "verified_target_or_media_required": "等待渠道或内容格式验证",
    "experiment_working_budget": "试验工作预算已预留或用完，等待新条件，同时保留回执检查",
    "experiment_limit": "该试验达到投入上限，准备比较其他办法",
    "all_work_waiting_or_saturated": "现有内容已备齐，其余任务尚未到期",
    "lane_temporarily_unavailable": "当前执行通道暂不可用，其他独立工作继续",
    "content_reserve_full": "已有待分发内容，先使用库存",
    "awaiting_business_information": "等待必要业务信息",
    "token_budget_insufficient": "本项工作所需额度暂不足",
    "model_not_configured": "等待当前模型的服务凭据，已有内容仍可分发",
    "model_action_required": "模型额度、凭据或模型权限需要处理，已有内容仍可分发",
    "awaiting_distribution_feedback": "已有可用稿件等待分发；通用延伸研究等待下游采用或新反馈",
    "recovering_prepared_publication": "继续核对原稿及原发布任务，不重复规划新稿",
    "public_shortlink_required": "等待平台配置可公开访问的 HTTPS 推广短链地址",
}


BLOCKED_REASONS = {
    "CONTENT_FACT_SOURCE_MISMATCH": "草稿中的事实引用与业务资料不符",
    "CONTENT_DUPLICATE_BODY": "生成的正文与已有稿件重复",
    "CONTENT_MEDIA_BRIEF_REQUIRED": "缺少符合目标格式的素材说明",
    "NO_VERIFIABLE_PUBLIC_OBSERVATION": "尚未找到可核实的公开信息",
    "OPERATING_SOURCE_MISMATCH": "研究结果与引用来源不符",
    "STRATEGY_SOURCE_MISMATCH": "策略依据与引用来源不符",
    "PUBLIC_PRODUCT_URL_REQUIRED": "缺少可公开访问的产品网址",
    "PUBLIC_ACCESS_CHALLENGE": "目标网站要求访问验证",
    "PUBLIC_CONTENT_UNAVAILABLE": "暂时无法读取目标网站的公开内容",
    "ACCOUNT_NEEDS_AUTH": "渠道账号需要重新验证登录或权限",
    "BROWSER_USE_UNAVAILABLE": "公开搜索遇到验证，备用浏览器探索尚未启用或安装",
    "BROWSER_UNAVAILABLE": "执行任务的浏览器暂不可用",
    "ModelUnavailable": "当前模型尚未配置服务凭据",
    "UnexpectedModelBehavior": "模型返回的内容未通过校验",
    "ModelHTTPError": "模型服务返回请求错误",
    "TimeoutError": "执行请求超时",
    "ValidationError": "执行数据未通过格式校验",
    "ModelRateLimitError": "模型服务暂时限制请求",
    "ReadTimeout": "读取响应超时",
}


def blocked_reason(result):
    reason = result.get("reason")
    if isinstance(reason, str) and reason.strip():
        return REASONS.get(reason) or BLOCKED_REASONS.get(reason) or reason.strip()
    for key in ("deferred_reason", "error_code", "error", "error_type"):
        value = result.get(key)
        if isinstance(value, str):
            label = REASONS.get(value) or BLOCKED_REASONS.get(value)
            if label:
                return label
    error_type = result.get("error_type")
    if isinstance(error_type, str) and error_type.isidentifier():
        return f"执行出错（{error_type}）"
    return "未记录具体原因"


def iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def periods(agent, business, now):
    zone = (
        agent["onboarding_state"].get("operating_timezone")
        or business.get("facts", {}).get("timezone")
        or "Asia/Shanghai"
    )
    try:
        local = now.astimezone(ZoneInfo(zone))
    except (ValueError, KeyError):
        zone = "Asia/Shanghai"
        local = now.astimezone(ZoneInfo(zone))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    return zone, day, week


def acquisition(c, agent, business, now):
    """Read-only boundary for the shortlink / event work in the other chat.

    Public configuration alone is not a successful collector health check. Until
    that subsystem exposes verified health, absent observations remain unknown.
    """
    from .analytics import public_origin
    from .growth_experiments import chamber_campaign
    from .pixel_events import configuration as pixel_configuration

    clicks_only = chamber_campaign(agent)
    connections = [] if clicks_only else all_rows(
        c,
        "SELECT * FROM lead.analytics_connections WHERE provider='lead_site' AND state<>'disconnected' ORDER BY selected_at DESC,created_at DESC",
    )

    def origin_key(value):
        parsed = urlsplit(value or "")
        return (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )

    origin = origin_key(business.get("product_url"))
    conn = next(
        (row for row in connections if origin_key(row["site_host"]) == origin), None
    )
    pixel = {"installed": False, "events": []} if clicks_only else pixel_configuration(c, conn)
    definitions = pixel.get("events", [])
    evidence = [] if not conn else all_rows(
        c,
        """SELECT event_name,definition_revision,
        min(received_at) AS first_received_at,max(received_at) AS last_received_at,
        count(*) AS live_count,
        count(*) FILTER(WHERE touchpoint_id IS NOT NULL) AS attributed_count
        FROM lead.pixel_receipts WHERE connection_id=%s AND counted AND scope_key='live'
        AND occurred_at>=%s GROUP BY event_name,definition_revision""",
        (conn["id"] if conn else None, now - timedelta(days=30)),
    )
    events = []
    for d in definitions:
        rows = [
            r
            for r in evidence
            if r["event_name"] == d["name"]
            and r["definition_revision"] == d["revision"]
        ]
        valid = bool(rows)
        semantic = d["name"] != "Activation" or bool(d.get("definition", "").strip())
        ready = bool(pixel["installed"] and valid and semantic)
        events.append(
            {
                "name": d["name"],
                "connection_id": str(conn["id"]),
                "label": d["label"],
                "revision": d["revision"],
                "method": d["method"],
                "ready": ready,
                "state": "ready"
                if ready
                else "site_unavailable"
                if not pixel["installed"]
                else "definition_required"
                if not semantic
                else "awaiting_verification",
                "trust": "server_verified"
                if d["method"] == "server"
                else "client_observed",
                "coverage_since": iso(
                    min((r["first_received_at"] for r in rows), default=None)
                ),
                "observed_count_30d": sum(r["live_count"] for r in rows)
                if valid
                else None,
                "attributed_count_30d": sum(r["attributed_count"] for r in rows)
                if valid
                else None,
            }
        )
    goal = configuration(agent, business)
    target = EVENT_NAMES.get(goal.get("objective_event"), goal.get("objective_event"))
    selected = next((e for e in events if e["name"] == target), None)
    ready_events = [e for e in events if e["ready"]]
    clicks = one(
        c,
        """SELECT count(*) FILTER(WHERE exclusion_reason IS NULL) AS filtered_clicks,
        count(*) AS requests,max(started_at) AS last_observed_at FROM lead.visit_sessions
        WHERE trust_level='redirect_observed' AND tracking_link_id IS NOT NULL AND started_at>=%s""",
        (now - timedelta(days=30),),
    )
    origin = public_origin()
    public = urlsplit(origin).hostname not in {"localhost", "127.0.0.1", "::1"}
    # Existing storage has no collector health/coverage contract. Do not infer a
    # measured zero merely from a configured URL or from installed site pixels.
    click_state = (
        "observed"
        if clicks["requests"]
        else "awaiting_health_contract"
        if public
        else "not_public"
    )
    return {
        "mode": "traffic_conversion" if ready_events else "traffic",
        "clicks_only": clicks_only,
        "mode_label": "引流＋转化" if ready_events else "引流",
        "site_state": "outside_objective" if clicks_only else "ready"
        if pixel["installed"]
        else "stale"
        if conn and conn.get("last_received_at")
        else "not_installed",
        "arrival_available": bool(pixel["installed"]),
        "events": events,
        "objective_event": target,
        "objective_ready": bool(selected and selected["ready"]),
        "conversion_optimization": bool(selected and selected["ready"]),
        "clicks": {
            "state": click_state,
            "filtered_clicks_30d": clicks["filtered_clicks"]
            if clicks["requests"]
            else None,
            "last_observed_at": iso(clicks["last_observed_at"]),
            "provider": "platform_shortlink",
            "coverage_verified": False,
        },
        "limits": "短链记录表示经过已知规则过滤的点击，不等于到站、人数或注册。转化仅使用当前事件定义的正式回执；未归因事件单列，不计算无同批分母的转化率。",
    }


def stage_for(kind):
    if kind in ("content", "content.prepare"):
        return "content"
    if kind == "publish" or kind.endswith(".publish") or kind == "community_progress":
        return "distribution"
    if kind in ("observation", "operating.observation", "operating.strategy"):
        return "observation"
    return "audience"


def prioritize(choices, mode, focus=None):
    """Adjust only admitted work; no plan can bypass grants, budgets or timing."""
    for candidate in choices:
        kind = candidate.kind
        if kind == "publish":
            candidate.priority += 12  # use prepared work before more research
        elif kind == "content":
            candidate.priority += 4
        elif kind == "operating.journey" and mode["conversion_optimization"]:
            candidate.priority += 8
        if kind in acquisition_strategy.WORK.get(mode.get("test_layer"), ()):
            candidate.priority += 14
        if focus and stage_for(kind) == focus and kind not in PLATFORM_KINDS:
            candidate.priority += 10
    return sorted(choices, key=lambda x: (-x.priority, x.key))


def distribution_channels(c):
    return all_rows(c, """
        SELECT s.platform,s.state,s.automation_enabled,t.id AS target_id,
          t.verified_permissions->'publishing_verified_formats' AS formats,
          EXISTS(SELECT 1 FROM lead.authorization_grants g WHERE g.target_id=t.id
            AND g.revoked_at IS NULL AND g.allowed_actions ? 'publish') AS granted,
          EXISTS(SELECT 1 FROM lead.platform_capabilities p WHERE p.platform=s.platform
            AND p.target_type=t.target_type AND p.action_type='publish' AND p.technical_state='verified'
            AND (p.permission_state='approved' OR p.permission_state='pending' AND lead.owner_browser_pilot_allowed(t.id))
            AND (s.platform='lab' OR p.adapter_version=t.verified_permissions->>'adapter_version')) AS supported,
          setup.result->>'creation_state' AS creation_state,setup.state AS setup_state
        FROM lead.social_accounts s
        LEFT JOIN lead.account_targets t ON t.account_id=s.id AND t.state='active'
        LEFT JOIN lead.tasks setup ON setup.type='account.prepare' AND setup.workflow_id=
          'account.prepare:' || s.id::text || ':' || s.external_account_id
        WHERE s.state<>'disconnected'""")


def distribution_blocker(channels, platform, kind):
    accounts = [r for r in channels if r["platform"] == platform]
    connected = [r for r in accounts if r["state"] == "connected"]
    if not connected:
        return "还没有可用的渠道账号", "请在社媒账号中检查连接状态，按提示登录或恢复账号。"
    enabled = [r for r in connected if r["automation_enabled"]]
    if not enabled:
        return "账号尚未开启自主运营", "如需发布，请在社媒账号中为这个渠道开启自主运营。"
    targets = [r for r in enabled if r["target_id"]]
    if not targets:
        if platform == "telegram" and any(r["creation_state"] in ("unknown", "dispatching") for r in enabled):
            return "平台已尝试创建 Telegram 频道，尚未确认是否建成", "平台需要用已连接的账号查找原频道，确认可以发帖后再安排这篇稿件。你无需重新建频道或提交稿件。"
        if any(r["setup_state"] == "waiting_user" for r in enabled):
            return "频道设置需要处理", "请打开社媒账号查看频道设置提示，完成提示中的操作后再继续。"
        return "还没有可用的发布位置", "需要先在这个账号下确认发布到哪个频道或主页。"
    granted = [r for r in targets if r["granted"]]
    if not granted:
        return "发布位置已有记录，但尚未获得发帖授权", "请查看这个渠道的账号设置，确认是否允许 Agent 代为发帖。"
    supported = [r for r in granted if r["supported"]]
    if not supported:
        return "当前发布方式尚未通过验证", "平台需要核对这个渠道允许的发布方式，并验证当前连接。"
    formats = {fmt for row in supported for fmt in row["formats"] or []}
    if platform != "lab" and (not formats or (kind and kind not in formats)):
        label = {"text": "文字", "image": "图片", "video": "视频"}.get(kind, "这类内容")
        return f"这个发布位置尚未验证能否发送{label}", f"平台需要验证{label}发布，并核对发布结果。稿件已保存。"
    return None


def business_tasks(c, agent, business, day, end):
    rows = all_rows(
        c,
        """SELECT t.id,t.type,t.state,t.input,t.result,t.created_at,t.scheduled_at,t.started_at,t.completed_at,
        a.state AS action_state,a.external_url,a.external_id,ci.body AS content
        FROM lead.tasks t LEFT JOIN LATERAL(SELECT state,external_url,external_id FROM lead.actions
          WHERE task_id=t.id ORDER BY created_at DESC LIMIT 1) a ON true
        LEFT JOIN lead.content_items ci ON ci.id=NULLIF(t.input->>'content_id','')::uuid
        WHERE (t.type IN ('content.prepare','community.discover','community.engage') OR t.type IN ('operating.journey','operating.opportunities','operating.validate','operating.strategy') OR t.type LIKE '%%.publish')
        AND coalesce(t.input->>'bootstrap_verification','false')<>'true'
        AND (coalesce(nullif(t.result->'delivery'->>'public_at','')::timestamptz,t.completed_at) >= %s AND coalesce(nullif(t.result->'delivery'->>'public_at','')::timestamptz,t.completed_at) < %s OR
          (t.input->>'config_revision'=%s OR t.input->>'config_revision' IS NULL)
          AND (t.input->>'business_profile_id'=%s OR t.input->>'business_profile_id' IS NULL)
          AND (t.state IN ('queued','planned','running','reconciling','waiting_user')
          OR t.type='content.prepare' AND t.result->>'status'='ready')
          OR t.type LIKE '%%.publish' AND EXISTS (
              SELECT 1 FROM lead.tracking_links l JOIN lead.visit_sessions v ON v.tracking_link_id=l.id
              WHERE l.content_id=ci.id AND v.agent_id=t.agent_id AND l.agent_id=t.agent_id
              AND v.trust_level='redirect_observed' AND v.exclusion_reason IS NULL
              AND v.started_at>=%s AND v.started_at<%s))
        AND (t.type NOT LIKE 'operating.%%' OR t.input->>'progress_version'='1')
        ORDER BY t.created_at,t.id""",
        (day, end, str(agent["config_revision"]), str(business["id"]), day, end),
    )
    items = []
    channels = None
    publish_lanes = {}
    eligible = []
    for row in rows:
        payload, result = row["input"] or {}, row["result"] or {}
        from .publication_delivery import LABELS as DELIVERY_LABELS
        from .publication_delivery import is_public

        platform = payload.get("platform") or row["type"].split(".")[0]
        pending_delivery = row["action_state"] == "confirmed" and not is_public(
            platform, result.get("delivery")
        )
        completed_at = (
            datetime.fromisoformat(result["delivery"]["public_at"])
            if (result.get("delivery") or {}).get("public_at")
            else row["completed_at"]
        )
        stage = stage_for(row["type"])
        state = row["state"]
        if stage == "distribution":
            state = (
                "waiting"
                if pending_delivery
                else "done"
                if row["action_state"] == "confirmed"
                else "reconciling"
                if row["action_state"] in ("unknown", "dispatching")
                or state in ("reconciling", "succeeded")
                else "running"
                if state == "running"
                else "blocked"
                if state in ("failed", "waiting_user")
                else "ready"
            )
        else:
            state = (
                "done"
                if state == "succeeded"
                else "blocked"
                if state in ("failed", "waiting_user")
                else "running"
                if state == "running"
                else "waiting"
                if row["scheduled_at"]
                and row["scheduled_at"] > datetime.now(timezone.utc)
                else "ready"
            )
        if row["state"] == "canceled" and state not in ("done", "reconciling"):
            state = "canceled"
        title = (
            result.get("title")
            or (row["content"] or {}).get("title")
            or payload.get("title")
            or payload.get("topic")
            or {
                "community.discover": "寻找 Telegram 讨论群并核对群规",
                "operating.journey": "验证当前业务入口与使用路径",
                "operating.opportunities": "寻找与目标受众有关的新触达机会",
                "operating.validate": "核对新线索是否值得推进",
                "operating.strategy": "将新证据转成可验证的业务假设",
            }.get(row["type"], LABELS[stage])
        )
        next_step = None
        if row["type"] == "community.discover" and result.get("candidates"):
            growth = agent["onboarding_state"].get("community_growth") or {}
            count = len(result["candidates"])
            next_step = (f"已发现 {count} 个候选群，按群核对相关性、群规和账号委托。"
                         if growth.get("enabled") and growth.get("mode") == "contextual_outreach"
                         else f"已发现 {count} 个候选群，等待所有者启用并授权入群观察。")
        if row["type"] == "community.engage":
            outcome = result.get("outcome") or {}
            title = "社群跟进 · " + (payload.get("candidate", {}).get("name") or payload.get("candidate", {}).get("telegram_url") or "Telegram")
            next_step = outcome.get("reason") or (result.get("decision") or {}).get("reason")
            if result.get("join_state") == "pending_approval":
                state, next_step = "waiting", "入群申请已提交，等待管理员批准后核对成员身份。"
            elif result.get("reply_state") in ("unknown", "dispatching") or result.get("join_state") in ("unknown", "dispatching"):
                state, next_step = "reconciling", "核对原操作回执，避免重复提交。"
            elif outcome.get("state") == "blocked":
                state = "blocked"
        items.append(
            {
                "id": str(row["id"]),
                "stage": stage,
                "title": title,
                "state": state,
                "next_step": next_step,
                "platform": ("telegram" if row["type"].startswith("community.") else payload.get("platform"))
                or (
                    row["type"].split(".")[0]
                    if row["type"].endswith(".publish")
                    else None
                ),
                "scheduled_at": iso(row["scheduled_at"]),
                "completed_at": iso(completed_at),
                "carried_over": row["created_at"] < day,
                "delivery": result.get("delivery"),
                "reason": DELIVERY_LABELS.get(
                    (result.get("delivery") or {}).get("state"),
                    DELIVERY_LABELS["unknown"],
                )
                if pending_delivery
                else blocked_reason(result)
                if state == "blocked"
                else REASONS.get(
                    result.get("reason"),
                    "等待原动作回执，平台核对中"
                    if state == "reconciling"
                    else "",
                ),
                "external_url": row["external_url"],
                "receipt": row["external_id"]
                if row["action_state"] == "confirmed"
                else None,
                "evidence_task_id": str(row["id"]),
                "completed_today": bool(
                    completed_at and day <= completed_at < end and state == "done"
                ),
            }
        )
        if row["type"] == "content.prepare" and result.get("status") == "ready":
            if channels is None:
                from .operating_kernel import (
                    blocked,
                    eligible_publish_targets,
                    publication_lane_key,
                )

                channels = distribution_channels(c)
                eligible = eligible_publish_targets(c, blocked(c))
                kernel = agent["onboarding_state"].get("operating_kernel", {})
                if (kernel.get("source_revision") == agent["config_revision"]
                    and kernel.get("source_control") == agent["control_version"]
                    and kernel.get("business_profile_id") == str(business["id"])):
                    publish_lanes = kernel.get("lanes", {})
            blocker = distribution_blocker(channels, platform, payload.get("format"))
            targets = [t for t in eligible if t["platform"] == platform and payload.get("format") in (t["formats"] or [])]
            if not blocker and targets and all(t["channel_language"] and t["channel_language"] != payload.get("channel_language") for t in targets):
                blocker = ("稿件语言未标记" if not payload.get("channel_language") else "稿件语言与频道不匹配",
                           "选稿时只会复用语言与目标频道一致的稿件。")
            if not blocker and not targets:
                blocker = ("暂无可选的发布位置", "等平台核对当前授权和在途操作后，再检查可用频道。")
            matching = [t for t in targets if not t["channel_language"] or t["channel_language"] == payload.get("channel_language")]
            checks = [publish_lanes.get(publication_lane_key([t]), {}).get("next_at") for t in matching]
            check_at = min(checks) if checks and all(checks) else None
            due = check_at if check_at and datetime.fromisoformat(check_at) > datetime.now(timezone.utc) else None
            state = (
                "blocked"
                if blocker
                else "waiting"
                if due
                else "ready"
            )
            items.append(
                {
                    "id": "distribute:" + str(row["id"]),
                    "stage": "distribution",
                    "title": "分发：" + title,
                    "state": state,
                    "platform": payload.get("platform"),
                    "scheduled_at": None,
                    "channel_check_at": None if blocker else due,
                    "completed_at": None,
                    "carried_over": row["created_at"] < day,
                    "completed_today": False,
                    "next_step": blocker[1] if blocker else None,
                    "reason": blocker[0]
                    if blocker
                    else "等待渠道复查；届时核对条件并选稿，尚未为此稿排期"
                    if state == "waiting"
                    else "已有内容可复用，执行前核对当前权限与窗口",
                    "external_url": None,
                    "receipt": None,
                    "evidence_task_id": str(row["id"]),
                }
            )
    return items


def platform_issues(c, agent, mode, now):
    from .outbound_links import configured_problem

    issues = []
    shortlink_problem = configured_problem()
    if shortlink_problem or mode["clicks"]["state"] != "observed":
        issues.append(
            {
                "key": "shortlink",
                "owner": "platform",
                "title": "推广短链公网入口未就绪"
                if shortlink_problem
                else "短链采集待接通或验证",
                "impact": "新带短链分发等待公网地址；保留备稿，其他独立工作继续"
                if shortlink_problem
                else "引流数量暂不可判断；分发与内容工作继续",
                "next_action": "由平台配置并验证公网 HTTPS 短链入口，不让 Agent 重复排查"
                if shortlink_problem
                else "对接短链会话提供的采集健康与覆盖接口",
            }
        )
    if not mode.get("clicks_only") and not mode["arrival_available"]:
        issues.append(
            {
                "key": "pixel",
                "owner": "platform",
                "title": "网站像素未验证或已过期",
                "impact": "当前按引流模式运营，不推断站内行为",
                "next_action": "在网站像素设置中查看安装和验证状态",
            }
        )
    if mode["mode"] == "traffic_conversion" and not mode["objective_ready"]:
        issues.append(
            {
                "key": "objective_event",
                "owner": "platform",
                "title": "经营目标事件尚未就绪",
                "impact": "不据缺失转化评价内容优劣",
                "next_action": "在完整计划中选择目标事件，并在网站像素中验证同一事件",
            }
        )
    for row in all_rows(
        c,
        """SELECT s.platform,s.state,s.automation_enabled,
        (SELECT t.verified_permissions->'publication_delivery' FROM lead.account_targets t WHERE t.account_id=s.id AND t.state='active' AND t.verified_permissions ? 'publication_delivery' ORDER BY t.id LIMIT 1) AS delivery,
        (EXISTS(SELECT 1 FROM lead.platform_capabilities p WHERE p.platform=s.platform AND p.action_type='publish' AND p.permission_state='approved') OR EXISTS(SELECT 1 FROM lead.account_targets pt WHERE pt.account_id=s.id AND lead.owner_browser_pilot_allowed(pt.id))) AS permission_approved,
        EXISTS(SELECT 1 FROM lead.account_targets t WHERE t.account_id=s.id AND t.state='active'
          AND jsonb_array_length(coalesce(t.verified_permissions->'publishing_verified_formats','[]'))>0) AS verified
        FROM lead.social_accounts s WHERE s.state<>'disconnected' AND s.platform<>'lab'""",
    ):
        if (
            row["automation_enabled"]
            and row["state"] == "connected"
            and row["permission_approved"]
            and row["delivery"]
            and row["delivery"].get("state") != "public"
        ):
            from .publication_delivery import LABELS as DELIVERY_LABELS

            issues.append(
                {
                    "key": "channel_delivery:" + row["platform"],
                    "owner": "platform",
                    "title": row["platform"]
                    + " "
                    + DELIVERY_LABELS.get(
                        row["delivery"].get("state"), DELIVERY_LABELS["unknown"]
                    ),
                    "impact": "保留原提交与稿件，尚不计公开推广成果；其他渠道按自身条件推进",
                    "next_action": "平台只读复查同一条内容的审核与可见性；不会重新上传或重发",
                }
            )
            continue
        if row["automation_enabled"] and (
            row["state"] != "connected"
            or not row["verified"]
            or not row["permission_approved"]
        ):
            issues.append(
                {
                    "key": "channel:" + row["platform"],
                    "owner": "platform",
                    "title": row["platform"]
                    + (
                        " 平台发布方式尚未开放"
                        if not row["permission_approved"]
                        else " 发布能力待恢复"
                    ),
                    "impact": "该渠道暂不发布；独立渠道和内容任务继续",
                    "next_action": "核对适用地区与平台允许的发布方式，再进行实际格式验证"
                    if not row["permission_approved"]
                    else "由平台恢复账号身份、目标权限与格式验证",
                }
            )
    from .admin_settings import effective
    from .operating_admission import model_readiness

    models, version = effective(c, "models", str(agent["id"]))
    missing = [p for p, r in model_readiness(models).items() if not r["configured"]]
    if missing:
        issues.append(
            {
                "key": "model_credentials",
                "owner": "platform",
                "title": "当前模型尚未配置服务凭据",
                "impact": "新模型工作等待，已有内容及发布回执核对继续",
                "next_action": "在本机配置当前模型服务的凭据，保存后自动重新检查",
            }
        )
    backoff = agent["onboarding_state"].get("model_backoff", {})
    from .model_backoff import active

    if not missing and active(
        backoff, version, [r["model"] for r in model_readiness(models).values()]
    ):
        issues.append(
            {
                "key": "model",
                "owner": "platform",
                "title": "模型额度、凭据或模型权限需要处理"
                if backoff.get("requires_owner_action")
                else "模型服务暂时限流",
                "impact": "需要模型的新工作等待，已有内容仍按节奏分发",
                "next_action": "处理模型服务配置或额度后自动重新检查"
                if backoff.get("requires_owner_action")
                else "平台按退避时间自动恢复",
                "retry_at": backoff["retry_at"],
            }
        )
    return issues


def assemble(c, agent, business, now=None):
    now = now or datetime.now(timezone.utc)
    zone, day, week = periods(agent, business, now)
    mode = acquisition(c, agent, business, now)
    goal = configuration(agent, business)
    strategy = acquisition_strategy.current(c, agent, business, goal, mode)
    target = next((event for event in mode["events"] if event["name"] == mode["objective_event"]), None)
    strategy["results_30d"] = acquisition_strategy.target_results(
        c, {**target, "first_definition": strategy["first_definition"]} if target else None,
        now - timedelta(days=30), now)
    if mode.get("clicks_only"):
        strategy.update(test_layer="distribution", layer_label="触达与分发", measurement="独立产品短链的有效点击", first_step="点击推广帖的产品短链", first_definition=None, cost_ceiling=None,
                        next_action="跨场景触达与回访，按相同帖龄的有效点击分配下一次机会。")
    mode["test_layer"] = strategy["test_layer"]
    items = business_tasks(c, agent, business, day, day + timedelta(days=1))
    kernel = agent["onboarding_state"].get("operating_kernel", {})
    deferred = {x["key"]: x for x in kernel.get("deferred_candidates", [])}
    for item in items:
        wait = deferred.get(item["id"])
        rating = next(
            (x for x in kernel.get("candidates", []) if x["key"] == item["id"]), None
        )
        item["priority"] = (
            rating["priority"]
            if rating
            else 100
            if item["id"].startswith("distribute:")
            else 85
            if item["stage"] == "content"
            else 70
        )
        if item["state"] == "ready" and wait:
            item["state"] = "waiting"
            item["reason"] = REASONS.get(wait.get("reason"), "等待额度或执行条件恢复")
            item["scheduled_at"] = wait.get("retry_at") or kernel.get("next_at")
    inventory = [
        {"title": i["title"], "platform": i["platform"]}
        for i in items
        if i["stage"] == "content" and i["state"] == "done"
    ]
    focus = agent["onboarding_state"].get("plan_focus", "distribution")
    selected = kernel.get("selected")
    last = kernel.get("last_result", {})
    current_kind = (selected or {}).get("kind", "")
    platform_work = current_kind in PLATFORM_KINDS
    running = next((i for i in items if i["state"] in ("running", "reconciling")), None)
    reason = last.get("reason", "all_work_waiting_or_saturated")
    current = {
        "state": "paused"
        if agent["desired_state"] != "running"
        else "running"
        if running
        else "waiting",
        "stage": running["stage"] if running else None,
        "title": running["title"]
        if running
        else REASONS.get(reason, "等待下一项满足条件的业务工作"),
        "next_check_at": kernel.get("next_at"),
        "platform_work": platform_work,
        "next_action": "完成当前业务工作并保留证据"
        if running
        else "到期后重新选择可执行工作；出现新回执或新结果时调整未开始任务",
    }
    if current["state"] == "waiting" and reason == "all_work_waiting_or_saturated":
        recent_groups = one(
            c,
            """SELECT jsonb_array_length(result->'candidates') AS count FROM lead.tasks
            WHERE type='community.discover' AND state='succeeded' AND input->>'config_revision'=%s
            AND completed_at>now()-interval '7 days' AND jsonb_array_length(result->'candidates')>0
            ORDER BY completed_at DESC LIMIT 1""",
            (str(agent["config_revision"]),),
        )
        config = agent["onboarding_state"].get("community_growth") or {}
        if recent_groups and config.get("mode", "discovery") == "discovery":
            current.update(
                title=f"最近一批发现 {recent_groups['count']} 个群，入群观察待授权",
                next_action="公开找群和其他渠道的只读研究按各自排期推进。入群观察需单独启用社群参与。",
            )
    issues = platform_issues(c, agent, mode, now)
    objectives = [
        {
            "stage": "audience",
            "title": "验证目标受众最关心的使用场景",
            "detail": goal["audience"] or "依据已保存的业务介绍，逐步验证受众假设",
            "success": "取得可引用的新需求证据；无新证据时不重复研究",
        },
        {
            "stage": "content",
            "title": "用具体内容解释产品与下一步",
            "detail": "复用现有稿件，围绕单一需求说明适用条件和业务入口",
            "success": "具体稿件和素材可使用，事实有来源；准备完成不等于引流成功",
        },
        {
            "stage": "distribution",
            "title": "在已授权渠道获得可追踪引流",
            "detail": "使用现有渠道、符合窗口的内容与平台短链",
            "success": "外部回执确认发布，另行观察短链点击",
        },
        {
            "stage": "observation",
            "title": "比较引流与目标事件效果"
            if mode["conversion_optimization"]
            else "比较引流效果并调整表达",
            "detail": "保留相同渠道与观察窗口；证据不足时继续观察",
            "success": "使用真实点击和已验证事件"
            if mode["conversion_optimization"]
            else "使用真实点击；站内转化缺失时保持未知",
        },
    ]
    if inventory:
        objectives[1]["detail"] = (
            "优先复用："
            + "；".join(i["title"] for i in inventory)
            + "。只在有新需求或库存被消耗时补充内容。"
        )
    destinations = all_rows(
        c,
        "SELECT DISTINCT s.platform,t.display_name FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.state='active' AND s.state='connected' AND s.automation_enabled AND jsonb_array_length(coalesce(t.verified_permissions->'publishing_verified_formats','[]'))>0 ORDER BY s.platform,t.display_name",
    )
    if destinations:
        objectives[2]["detail"] = (
            "按原授权节奏使用："
            + "；".join(d["platform"] + " · " + d["display_name"] for d in destinations)
            + "。每次分发关联具体内容与外部回执。"
        )
    steps = []
    for stage in STAGES:
        group = [i for i in items if i["stage"] == stage]
        done = sum(i["completed_today"] for i in group)
        active = any(i["state"] in ("ready", "running") for i in group)
        state = (
            "running"
            if any(i["state"] == "running" for i in group)
            else "ready"
            if active
            else "waiting"
            if any(i["state"] in ("waiting", "blocked", "reconciling") for i in group)
            else "done"
            if group and done == len(group)
            else "observing"
            if stage == "observation"
            else "standby"
        )
        steps.append(
            {
                "key": stage,
                "title": LABELS[stage],
                "state": state,
                "done": done,
                "total": len(group),
                "items": group,
            }
        )
    return {
        "timezone": zone,
        "mode": mode,
        "business_profile_id": str(business["id"]),
        "config_revision": agent["config_revision"],
        "preferences_revision": agent["onboarding_state"].get(
            "plan_preferences_revision", 0
        ),
        "strategy": strategy,
        "week": {
            "period": week.date().isoformat(),
            "ends_on": (week + timedelta(days=6)).date().isoformat(),
            "objective": goal["objective"],
            "audience": goal["audience"],
            "strategy_version": strategy["version"],
            "initiatives": objectives,
            "content_examples": inventory,
            "focus": focus,
        },
        "day": {
            "period": day.date().isoformat(),
            "steps": steps,
            "completed": sum(i["completed_today"] for i in items),
            "total": len(items),
            "current": current,
            "queue": sorted(
                items,
                key=lambda i: (
                    {
                        "running": 0,
                        "ready": 1,
                        "waiting": 2,
                        "reconciling": 3,
                        "blocked": 4,
                        "done": 5,
                        "canceled": 6,
                    }[i["state"]],
                    -i["priority"],
                    i["scheduled_at"] or "",
                ),
            ),
        },
        "platform_issues": issues,
        "continuous_operation": True,
        "fixed_task_limit": None,
        "ordering": "先满足授权、身份、格式、时段与预算，再按已有内容可分发、时效、目标价值、证据和成本排序。受阻任务保留，其他可执行任务继续。",
        "updated_at": now.isoformat(),
    }


def save(c, aid, plan):
    """Called with the agent row locked by the persistent supervisor."""
    for horizon in ("week", "day"):
        part = plan[horizon]
        previous = one(
            c,
            "SELECT id,body FROM lead.memory_entries WHERE kind='observation' AND body->>'type'='operating_plan' AND body->>'horizon'=%s AND body->>'period'=%s AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (horizon, part["period"]),
        )
        material = {
            "source": plan["business_profile_id"],
            "revision": plan["config_revision"],
            "timezone": plan["timezone"],
            "mode": plan["mode"]["mode"],
            "objective_event": plan["mode"]["objective_event"],
            "objective_ready": plan["mode"]["objective_ready"],
            "objective": plan["week"]["objective"],
            "audience": plan["week"]["audience"],
            "issues": [i["key"] for i in plan["platform_issues"]],
            "focus": plan["week"]["focus"],
            "strategy_version": plan["strategy"]["version"],
            "agenda": {
                "initiatives": plan["week"]["initiatives"],
                "content": plan["week"]["content_examples"],
            },
            "results": {
                "clicks": plan["mode"]["clicks"]["filtered_clicks_30d"],
                "events": [
                    (e["name"], e["revision"], e["ready"], e["attributed_count_30d"])
                    for e in plan["mode"]["events"]
                ],
            },
        }
        if horizon == "day":
            material["work"] = [(i["id"], i["state"]) for i in part["queue"]]
        key = digest(material)
        if previous and previous["body"]["fingerprint"] == key:
            part.update(
                {"id": str(previous["id"]), "version": previous["body"]["version"]}
            )
            continue
        old = previous["body"] if previous else {}
        version = old.get("version", 0) + 1
        reason = (
            "建立本期计划"
            if not previous
            else "数据接入状态改变"
            if old.get("mode") != material["mode"]
            or old.get("objective_ready") != material["objective_ready"]
            else "业务方向或来源更新"
            if old.get("source") != material["source"]
            or old.get("revision") != material["revision"]
            else "平台依赖变化"
            if old.get("issues") != material["issues"]
            else "出现新的引流或事件结果"
            if old.get("results") != material["results"]
            else "调整业务优先方向"
            if old.get("focus") != material["focus"]
            else "执行结果或候选业务工作变化"
        )
        pid = str(previous["id"]) if previous else uid()
        body = {
            "type": "operating_plan",
            "horizon": horizon,
            "period": part["period"],
            "version": version,
            "fingerprint": key,
            **material,
            "plan": part,
            "updated_at": plan["updated_at"],
        }
        if previous:
            c.execute(
                "UPDATE lead.memory_entries SET body=%s WHERE id=%s", (j(body), pid)
            )
        else:
            c.execute(
                "INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation) VALUES(%s,%s,%s,'observation',%s,'[]','system_observed')",
                (pid, current_tenant(), aid, j(body)),
            )
        change = {
            "type": "operating_plan_change",
            "plan_id": pid,
            "horizon": horizon,
            "period": part["period"],
            "version": version,
            "reason": reason,
            "mode": material["mode"],
            "previous_mode": old.get("mode"),
            "completed": part.get("completed"),
            "total": part.get("total"),
            "at": plan["updated_at"],
            "changes": {
                k: {"from": old.get(k), "to": material.get(k)}
                for k in (
                    "mode",
                    "objective",
                    "audience",
                    "objective_event",
                    "objective_ready",
                    "focus",
                    "strategy_version",
                    "issues",
                    "results",
                )
                if old.get(k) != material.get(k)
            },
        }
        if horizon == "day":
            previous_work, next_work = dict(old.get("work", [])), dict(material["work"])
            change["task_changes"] = [
                {"id": key, "from": previous_work.get(key), "to": next_work.get(key)}
                for key in previous_work.keys() | next_work.keys()
                if previous_work.get(key) != next_work.get(key)
            ]
        c.execute(
            "INSERT INTO lead.memory_entries(tenant_id,agent_id,kind,body,source_refs,confirmation) VALUES(%s,%s,'observation',%s,'[]','system_observed')",
            (current_tenant(), aid, j(change)),
        )
        part.update({"id": pid, "version": version})
    return plan


def sync(c, aid, agent, business, now=None):
    if not business or not business["description"].strip():
        return {"state": "business_required"}
    plan = save(c, aid, assemble(c, agent, business, now))
    strategy = plan["strategy"]
    acquisition_strategy.persist(c, aid, strategy)
    signal = acquisition_strategy.review_signal(plan["mode"])
    if signal != strategy["review_signal"] and agent["desired_state"] == "running":
        evidence = one(c, """SELECT id FROM lead.tasks WHERE type IN ('operating.opportunities','operating.validate')
            AND state='succeeded' AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
            ORDER BY completed_at DESC LIMIT 1""", (str(business["id"]), str(agent["config_revision"])))
        if evidence:
            from .operating_feedback import enqueue
            enqueue(c, aid, agent, business, "strategy", "依据现有需求证据建立当前获客策略",
                    work_key=f"alternatives:acquisition:{business['id']}:{agent['config_revision']}:{plan['preferences_revision']}:{agent['onboarding_state'].get('operating_contract', {}).get('revision', 0)}:{signal}")
    return plan


def execution_reference(plan):
    return {
        "week_id": plan["week"]["id"],
        "week_version": plan["week"]["version"],
        "day_id": plan["day"]["id"],
        "day_version": plan["day"]["version"],
        "mode": plan["mode"]["mode"],
        "focus": plan["week"]["focus"],
        "strategy_version": plan["strategy"]["version"],
        "test_layer": plan["strategy"]["test_layer"],
        "acquisition_strategy": {k: plan["strategy"][k] for k in ("audience", "trigger", "source", "promise", "proof", "first_step", "next_action", "objective_event")},
    }


def brief(c):
    """Small plan context for actual content/research, not the whole dashboard."""
    row = one(
        c,
        "SELECT id,body FROM lead.memory_entries WHERE kind='observation' AND body->>'type'='operating_plan' AND body->>'horizon'='week' AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
    )
    if not row:
        return None
    body = row["body"]
    return {
        "plan_id": str(row["id"]),
        "version": body["version"],
        "period": body["period"],
        "objective": body["objective"],
        "audience": body["audience"],
        "mode": body["mode"],
        "objective_ready": body["objective_ready"],
        "initiatives": body["plan"]["initiatives"],
        "strategy_version": body.get("strategy_version"),
    }


class AcquisitionCriteria(BaseModel):
    first_definition: str = Field(default="", max_length=300)
    cost_ceiling: str = Field(default="", max_length=150)
    quality: str = Field(default="", max_length=300)


class PlanPreference(BaseModel):
    acquisition_criteria: AcquisitionCriteria | None = None
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    objective_event: str | None = Field(
        default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
    )
    focus: str = Field(
        default="distribution", pattern=r"^(audience|content|distribution|observation)$"
    )


@router.post("/api/agents/{aid}/plans/preferences")
def preferences(aid: UUID, body: PlanPreference):
    aid = str(aid)
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        revision = agent["onboarding_state"].get("plan_preferences_revision", 0)
        if revision != body.expected_revision:
            raise HTTPException(409, "运营设置已更新，请刷新后再保存")
        name = EVENT_NAMES.get(body.objective_event, body.objective_event)
        if name and not one(
            c, "SELECT 1 FROM lead.pixel_definitions WHERE name=%s", (name,)
        ):
            raise HTTPException(422, "请选择本 Agent 已配置的业务事件")
        contract = {
            **agent["onboarding_state"].get("operating_contract", {}),
            "objective_event": name,
        }
        criteria = body.acquisition_criteria.model_dump() if body.acquisition_criteria else dict(agent["onboarding_state"].get("acquisition_criteria") or {})
        if name != agent["onboarding_state"].get("operating_contract", {}).get("objective_event") and not body.acquisition_criteria:
            criteria["first_definition"] = ""
        business = one(c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
        if body.acquisition_criteria and business:
            scoped = acquisition(c, agent, business, datetime.now(timezone.utc))
            event = next((e for e in scoped["events"] if e["name"] == name), None)
            if criteria["first_definition"] and (not event or event["method"] != "server"):
                raise HTTPException(422, "首次用户口径需要选择当前业务的服务端事件")
            criteria["event_key"] = [event["connection_id"], name, event["revision"]] if event else None
        contract["revision"] = contract.get("revision", 0) + 1
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s,version=version+1 WHERE id=%s",
            (
                j(
                    {
                        "acquisition_criteria": criteria,
                        "operating_contract": contract,
                        "plan_focus": body.focus,
                        "plan_preferences_revision": revision + 1,
                    }
                ),
                aid,
            ),
        )
        audit(
            c,
            aid,
            "operating.plan.preferences",
            {"objective_event": name, "focus": body.focus},
        )
        updated = require_agent(c, aid)
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if business:
            sync(c, aid, updated, business)
        emit(c, aid, "supervisor.wake")
    return {"ok": True}


@router.get("/api/agents/{aid}/plans")
def status(aid: UUID, today_only: bool = False):
    aid = str(aid)
    with tx(aid) as c:
        agent = require_agent(c, aid)
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if not business or not business["description"].strip():
            return {"state": "business_required"}
        from .telegram_group_board import report as group_report

        now = datetime.now(timezone.utc)
        zone, day, _ = periods(agent, business, now)
        board = group_report(c, agent, day, now)
        if today_only and board is not None:
            return {
                "state": "group_summary",
                "timezone": zone,
                "day": {"period": day.date().isoformat()},
                "telegram_group_board": board,
            }
        plan = assemble(c, agent, business, now)
        plan["telegram_group_board"] = board
        for horizon in ("week", "day"):
            stored = one(
                c,
                "SELECT id,body FROM lead.memory_entries WHERE kind='observation' AND body->>'type'='operating_plan' AND body->>'horizon'=%s AND body->>'period'=%s AND body->>'source'=%s AND body->>'revision'=%s AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
                (horizon, plan[horizon]["period"], plan["business_profile_id"], str(plan["config_revision"])),
            )
            plan[horizon].update(
                {
                    "id": str(stored["id"]) if stored else None,
                    "version": stored["body"]["version"] if stored else 0,
                    "persisted": bool(stored),
                }
            )
        plan["changes"] = [
            r["body"]
            for r in all_rows(
                c,
                "SELECT body FROM lead.memory_entries WHERE kind='observation' AND body->>'type'='operating_plan_change' AND deleted_at IS NULL ORDER BY created_at DESC,id DESC LIMIT 30",
            )
        ]
        from .budget import available

        plan["resources"] = available(c, aid)
        from .growth_experiments import enabled, report
        if enabled(agent):
            panel = report(c,agent,business,datetime.now(timezone.utc))
            plan["experiments"] = panel
            entries = [{"plan_id":e["id"],"version":1,"at":e["updated_at"],
                        "reason":f"获客试验 · {e['name']} · {e['reason']} 今日 {e['today_spent']} token，待结算 {e['today_held']}。"}
                       for e in panel["experiments"] if e.get("updated_at")]
            total = sum(r["spent"] for r in panel["costs"])
            tracked = sum(r["spent"] for r in panel["costs"] if r["bucket"])
            unknown = sum(r["unknown"] for r in panel["costs"])
            order = agent["onboarding_state"].get("operating_kernel",{}).get("trial_order",{})
            waits = [key for key,n in order.get("wait_cycles",{}).items() if n>=2]
            if waits:
                entries.insert(0,{"plan_id":"trial-wait","version":1,"at":datetime.now(timezone.utc).isoformat(),
                    "reason":f"{len(waits)} 项可执行工作已等待至少两轮。本轮选择：{order.get('selected_reason') or '等待新的执行条件'}；下一候选：{order.get('runner_up') or '待核对'}"})
            plan["changes"] = [{"plan_id":"trial-daily-ledger","version":panel["day"],"at":datetime.now(timezone.utc).isoformat(),
                                "reason":f"今日模型结算 {total} token，试点动作 {tracked}，旧记录或试点外 {total-tracked}。未知预留 {unknown}。"}
                               , *entries, *plan["changes"]]
        return enrich_view(c, aid, plan)
