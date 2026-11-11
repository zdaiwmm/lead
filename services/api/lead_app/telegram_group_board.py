"""Read-only Telegram group acquisition card for the agent workspace."""

from datetime import datetime, timedelta

from .community_growth import discovery_config
from .db import all_rows, one

# Membership is a stock, while candidate admission, joining, departure and
# messages are daily flows. Exit reviews also update the original join task.
# Count the review receipt once, on the day of the review.
METRICS = """
WITH candidate AS (
  SELECT DISTINCT ON (url) url, coalesce(item->>'name', url) AS name, t.completed_at AS at
  FROM lead.tasks t CROSS JOIN LATERAL jsonb_array_elements(t.result->'candidates') item
  CROSS JOIN LATERAL (SELECT lower(rtrim(item->>'telegram_url','/')) AS url) u
  WHERE t.agent_id=%(aid)s AND t.type='community.discover' AND url IS NOT NULL
    AND t.completed_at<=%(now)s AND (t.input->>'chamber_campaign' IS DISTINCT FROM 'true'
      OR item->>'scenario_priority' IN ('1','2') OR item->>'selection_basis'='observe_recent_chat')
  ORDER BY url, t.completed_at, t.id
), inbox AS (
  SELECT DISTINCT ON (account, receipt) account, receipt, name, at
  FROM (
    SELECT coalesce(nullif(input->>'external_account_id',''),input->>'account_id','') AS account,
      nullif(result->>'external_id','') AS receipt,
      coalesce(nullif(trim(input->>'name'),''),'Telegram 私聊') AS name,
      coalesce((result->>'reply_confirmed_at')::timestamptz,completed_at) AS at, id
    FROM lead.tasks WHERE agent_id=%(aid)s AND type='inbox.reply' AND result->>'reply_state'='confirmed'
  ) confirmed WHERE receipt IS NOT NULL AND at<=%(now)s
  ORDER BY account,receipt,at,id
), work AS (
  SELECT t.id, t.state, t.result r, t.input i, t.created_at,t.completed_at,
    coalesce(t.result->>'target_id',lower(rtrim(t.input#>>'{candidate,telegram_url}','/'))) AS peer,
    coalesce(nullif(trim(t.input#>>'{candidate,name}'),''),t.input#>>'{candidate,telegram_url}',t.result->>'target_id') AS name
  FROM lead.tasks t WHERE t.agent_id=%(aid)s AND t.type='community.engage'
), membership AS (
  SELECT DISTINCT ON (i->>'external_account_id',peer) * FROM work
  WHERE r ? 'join_state' AND peer IS NOT NULL
  ORDER BY i->>'external_account_id',peer,created_at DESC,id DESC
), receipts AS (
  SELECT 'candidate' AS metric,url AS key,name,at,'' AS detail FROM candidate
  UNION ALL
  SELECT 'inbox_reply',jsonb_build_array(account,receipt)::text,name,at,'' FROM inbox
  UNION ALL
  SELECT 'joined',peer,name,coalesce((r->>'join_confirmed_at')::timestamptz,(r->>'join_checked_at')::timestamptz,
    completed_at,(r->>'join_dispatched_at')::timestamptz),'已确认加入'
  FROM work WHERE r->>'join_submitted'='true' AND r->>'join_state'='confirmed'
  UNION ALL
  SELECT CASE WHEN r->>'join_state'='pending_approval' THEN 'pending' ELSE 'join_unknown' END,peer,name,created_at,CASE r->>'join_state' WHEN 'pending_approval'
    THEN '等待群管理员批准' ELSE '已发起，平台回执待核对' END
  FROM membership WHERE r->>'join_submitted'='true' AND r->>'join_state' IN ('unknown','dispatching','pending_approval')
  UNION ALL
  SELECT 'exit',peer,name,completed_at,'主动退出' FROM work
  WHERE r->>'leave_state'='confirmed' AND i ? 'membership_task_id'
  UNION ALL
  SELECT CASE WHEN i#>>'{reply,promote}'='true' THEN 'promotion' ELSE 'interaction' END,
    nullif(r->>'reply_external_id',''),name,
    coalesce((r->>'reply_dispatched_at')::timestamptz,completed_at),i#>>'{reply,body}'
  FROM work WHERE r->>'reply_state'='confirmed' AND i#>>'{reply,promote}' IN ('true','false')
), daily AS (
  SELECT DISTINCT ON (metric,key) metric,key,name,at,detail FROM receipts WHERE key IS NOT NULL
    AND (metric IN ('pending','join_unknown') OR at BETWEEN %(day)s AND %(now)s)
  ORDER BY metric,key,at DESC
), numbered AS (
  SELECT *,row_number() OVER (PARTITION BY metric ORDER BY at DESC,key) AS n FROM daily
), totals AS (
SELECT metric,count(*) AS total,jsonb_agg(jsonb_build_object('name',name,'at',at,'detail',detail)
  ORDER BY n) FILTER (WHERE n<=50) AS records FROM numbered GROUP BY metric
)
SELECT totals.*, latest.admitted_at FROM (SELECT max(at) AS admitted_at FROM candidate) latest
LEFT JOIN totals ON true
"""


def task_action(row):
    if row["type"] == "community.discover":
        return "搜索并核验 Telegram 候选群"
    name = row["name"] or "目标群"
    if row["reply_state"] in ("unknown", "dispatching"):
        return f"核对「{name}」的消息发送回执"
    if row["join_state"] in ("unknown", "dispatching", "pending_approval"):
        return f"核对「{name}」的入群结果"
    return f"跟进「{name}」的群内活动"


def clock_value(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def resident_activity(agent, work, now):
    state = agent["onboarding_state"].get("telegram_executor")
    if not isinstance(state, dict) or not state.get("mode"):
        return None
    heartbeat = clock_value(state.get("heartbeat_at"))
    # The dedicated executor publishes a heartbeat every 30 seconds.
    fresh = bool(heartbeat and now - timedelta(minutes=2) <= heartbeat <= now + timedelta(seconds=30))
    discovery = next((row for row in work if row["type"] == "community.discover"
                      and row["state"] == "running"), None)
    native = discovery and discovery["phase"] == "native_collect"
    search_after = clock_value(state.get("native_search_after")) if native else None
    sleeping = search_after is not None and search_after > now
    action = state.get("action")
    if agent["desired_state"] != "running":
        current, upcoming = "运营未启动或已暂停", "恢复运营后重新排期"
    elif not fresh:
        current, upcoming = "执行器心跳未更新", "等待执行器重新报告状态"
    elif action in {"discover", "checking"} or state.get("discovery_action") == "running":
        current = "正在检查执行条件" if action == "checking" else "正在推进找群研究"
        upcoming = None
    elif action not in {None, "waiting", "readonly_wait", "no_due_task"}:
        active = next((row for row in work if row["type"] == "community.engage"
                       and row["state"] == "running"), None)
        current = task_action(active) if active else "正在处理社群任务"
        upcoming = None
    elif state.get("last_result") == "backup_not_ready":
        current, upcoming = "等待备份就绪", "下次调度时复查备份"
    elif sleeping:
        current, upcoming = "等待 Telegram 原生搜索冷却", "冷却结束后复查原生搜索"
    else:
        current, upcoming = "等待下一次调度检查", None
    write_hold = None
    if fresh and agent["desired_state"] == "running" and state.get("writes_enabled") is False:
        if state.get("account_status") == "undetermined":
            write_hold = "尚未核实 Telegram 官方账号状态，入群和发言已暂停"
        elif state.get("account_status") == "limited":
            write_hold = "Telegram 官方状态显示账号受限，入群和发言已暂停"
        else:
            write_hold = "执行器当前只读，入群和发言已暂停"
    needs_status = (fresh and agent["desired_state"] == "running" and state.get("writes_enabled") is False
                    and state.get("account_status") == "undetermined" and state["mode"] == "production")
    return {
        "current": current, "next": upcoming, "write_hold": write_hold,
        "owner": "请用已绑定账号向 Telegram 官方 @SpamBot 发送 /start，等待状态回复。" if needs_status else None,
        "heartbeat_at": heartbeat, "next_check_at": clock_value(state.get("next_check_at")) if fresh else None,
        "native_search_after": search_after if sleeping and fresh else None,
        "discovery": {key: discovery[key] for key in
                      ("phase", "native_cursor", "query_count", "source_count", "checkpoint_at")}
                     if discovery else None,
    }


def progress(c, aid, now):
    args = {"aid": aid, "now": now}
    return one(c, """WITH pending AS (
        SELECT coalesce((result->>'receipt_review_after')::timestamptz,
                        (result->>'review_after')::timestamptz,created_at) AS due,
               (result->>'reply_state' IN ('unknown','dispatching') OR
                result->>'join_state' IN ('unknown','dispatching') OR state='reconciling') IS TRUE AS receipt
        FROM lead.tasks WHERE agent_id=%(aid)s AND type IN ('community.engage','inbox.reply')
          AND state IN ('running','reconciling')
          AND (state='reconciling' OR result->>'reply_state' IN ('unknown','dispatching')
            OR result->>'join_state' IN ('unknown','dispatching')
            OR (type='community.engage' AND input ? 'reply' AND result->>'reply_state' IS NULL))
    ), recent AS (
        SELECT created_at,result FROM lead.tasks WHERE agent_id=%(aid)s AND type='operating.decision'
          AND state='succeeded' AND completed_at<=%(now)s ORDER BY completed_at DESC LIMIT 3
    ) SELECT
        (SELECT max(coalesce((result->>'reply_confirmed_at')::timestamptz,completed_at,
            (result->>'reply_dispatched_at')::timestamptz)) FROM lead.tasks WHERE agent_id=%(aid)s
            AND type IN ('community.engage','inbox.reply') AND result->>'reply_state'='confirmed'
            AND nullif(coalesce(result->>'reply_external_id',result->>'external_id'),'') IS NOT NULL) AS confirmed_at,
        (SELECT min(due) FROM pending WHERE receipt AND due<=%(now)s) AS oldest_due_at,
        (SELECT max(p.occurred_at) FROM lead.pixel_receipts p JOIN lead.visit_sessions v
          ON v.agent_id=p.agent_id AND v.tenant_id=p.tenant_id AND v.source_session_id=p.click_id::text
          JOIN lead.tracking_links l ON l.id=v.tracking_link_id AND l.agent_id=v.agent_id
          WHERE p.agent_id=%(aid)s AND p.scope_key='live' AND p.event_name='PageView' AND p.counted
            AND v.source='redirect' AND v.exclusion_reason IS NULL AND l.platform='telegram') AS arrived_at,
        (SELECT count(*)=3 AND bool_and(
            (result#>>'{selected,kind}' IN ('operating.opportunities','operating.validate','operating.strategy','community_discovery','journey')
             OR result#>>'{cycle_result,reason}' IN ('operating_work_cooldown','all_work_waiting_or_saturated'))
            AND EXISTS (SELECT 1 FROM pending WHERE due<=recent.created_at AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(recent.result->'candidates') choice
                WHERE choice->>'kind'='community_progress' AND
                    (pending.receipt OR choice->>'delivery_ready'='true')))) FROM recent) AS delayed
    """, args)



def execution(c, aid, day, now):
    args = {"aid":aid, "day":day, "now":now}
    totals = one(c, """SELECT
        count(DISTINCT coalesce(result->>'target_id',lower(input#>>'{candidate,telegram_url}')))
            FILTER (WHERE result ? 'history_observation' AND
                coalesce((result->>'history_observed_at')::timestamptz,completed_at,created_at) BETWEEN %(day)s AND %(now)s) AS assessed,
        count(*) FILTER (WHERE result->>'join_submitted'='true' AND
            (result->>'join_dispatched_at')::timestamptz BETWEEN %(day)s AND %(now)s) AS joins_started,
        count(*) FILTER (WHERE result#>>'{draft_validation,state}'='accepted' AND
            (result#>>'{draft_validation,at}')::timestamptz BETWEEN %(day)s AND %(now)s) AS drafts_accepted,
        count(*) FILTER (WHERE (result->>'draft_repair_attempts')::int>0 AND
            (result#>>'{draft_validation,at}')::timestamptz BETWEEN %(day)s AND %(now)s) AS drafts_repaired,
        count(*) FILTER (WHERE result#>>'{draft_validation,state}'='rejected' AND
            (result#>>'{draft_validation,at}')::timestamptz BETWEEN %(day)s AND %(now)s) AS drafts_rejected,
        count(*) FILTER (WHERE result->>'reply_state'='failed' AND completed_at BETWEEN %(day)s AND %(now)s) AS sends_failed,
        count(DISTINCT coalesce(result->>'target_id',lower(input#>>'{candidate,telegram_url}')))
            FILTER (WHERE result#>>'{history_observation,joined}'='true'
              AND result#>>'{history_observation,can_reply}'='false'
              AND coalesce((result->>'history_observed_at')::timestamptz,completed_at,created_at)
                  BETWEEN %(day)s AND %(now)s) AS restricted,
        count(*) FILTER (WHERE state='failed' AND coalesce(completed_at,created_at) BETWEEN %(day)s AND %(now)s) AS tasks_failed
        FROM lead.tasks WHERE agent_id=%(aid)s AND type='community.engage'""", args)
    waiting = all_rows(c, """SELECT coalesce(input#>>'{candidate,name}','Telegram') AS name,
        CASE WHEN result->>'reply_state' IN ('unknown','dispatching') THEN '发送回执待核对'
             WHEN result->>'join_state'='pending_approval' THEN '等待管理员审核'
             ELSE '入群结果待核对' END AS state,
        coalesce(result#>>'{receipt_observation,at}',result->>'join_checked_at') AS checked_at,
        coalesce(CASE WHEN result->>'join_state'='pending_approval' THEN result->>'approval_review_after' END,result->>'receipt_review_after',
            (coalesce((result->>'join_checked_at')::timestamptz,completed_at,created_at) +
              CASE WHEN coalesce((result->>'join_checked_at')::timestamptz,completed_at,created_at) <
                   coalesce((result->>'join_dispatched_at')::timestamptz,created_at)+interval '1 hour'
                THEN interval '15 minutes'
                WHEN coalesce((result->>'join_checked_at')::timestamptz,completed_at,created_at) <
                   coalesce((result->>'join_dispatched_at')::timestamptz,created_at)+interval '1 day'
                THEN interval '2 hours' ELSE interval '24 hours' END)::text) AS next_at,
        result#>>'{receipt_observation,status}' AS check_state,
        coalesce(result#>>'{receipt_observation,detail}',result#>>'{outcome,reason}') AS reason
        FROM lead.tasks WHERE agent_id=%(aid)s AND type='community.engage'
          AND (state='reconciling' OR result->>'join_state'='pending_approval')
        ORDER BY coalesce((result->>'receipt_review_after')::timestamptz,created_at),id LIMIT 30""", args)
    timings = all_rows(c, """SELECT step->>'phase' AS phase,count(*) AS samples,
        round(avg((step->>'duration_ms')::numeric)/1000,2) AS average_seconds,
        round((percentile_cont(.95) WITHIN GROUP (ORDER BY (step->>'duration_ms')::numeric)/1000)::numeric,2) AS p95_seconds,
        count(*) FILTER (WHERE step->>'state'='failed') AS failures
        FROM lead.tasks t CROSS JOIN LATERAL jsonb_array_elements(t.result->'execution_steps') step
        WHERE t.agent_id=%(aid)s AND t.type='community.engage'
          AND (step->>'at')::timestamptz BETWEEN %(day)s AND %(now)s
        GROUP BY step->>'phase' ORDER BY step->>'phase'""", args)
    gates = all_rows(c, """SELECT coalesce(result#>>'{outcome,validation_rule}',
          result#>>'{draft_validation,rule}',result#>>'{outcome,reason}') AS rule,
          result#>>'{outcome,reason}' AS reason,count(*) AS count
        FROM lead.tasks WHERE agent_id=%(aid)s AND type='community.engage'
          AND completed_at BETWEEN %(day)s AND %(now)s
          AND result#>>'{outcome,state}' IN ('skipped','blocked')
        GROUP BY 1,2 ORDER BY count(*) DESC LIMIT 20""", args)
    from .community_participation import business_outcome

    outcomes = {}
    for task in all_rows(c, """SELECT state,result FROM lead.tasks WHERE agent_id=%s
        AND type='community.engage' AND completed_at BETWEEN %s AND %s""", (aid, day, now)):
        kind = business_outcome(task)
        outcomes[kind] = outcomes.get(kind, 0) + 1
    return {"totals":totals, "waiting":waiting, "timings":timings, "gates":gates, "outcomes":outcomes}


def participation_status(agent, config):
    """Distinguish configuration from a device's attested effective policy."""
    from pydantic import ValidationError

    from .community_participation import ParticipationPolicy

    device = agent["onboarding_state"].get("telegram_executor") or {}
    policy = device.get("participation_policy")
    try:
        if not isinstance(policy, dict) or not set(ParticipationPolicy.model_fields) <= policy.keys():
            policy = None
        else:
            policy = ParticipationPolicy.model_validate(policy).model_dump()
    except ValidationError:
        policy = None
    messages = ["主动发帖已开启" if config.allow_proactive_post else "主动发帖未开启",
                "产品推广已开启" if config.allow_promotion else "产品推广未开启"]
    if policy:
        if policy.get("quota_mode") == "per_action":
            messages.append(f"滚动 24 小时：入群最多 {policy['joins_per_24h']} 次，回复最多 {policy['replies_per_24h']} 次")
        else:
            messages.append(f"入群与回复合计每 24 小时最多 {policy['combined_per_24h']} 次")
        messages.append({"exact_faq":"答疑仅匹配群内原问题", "grounded_faq":"答疑可匹配近期未回答问题并引用群内原文",
                         "semantic_faq":"答疑可理解问题后引用群内原文，仍需有效的内容处理授权"}.get(policy.get("answer_mode"), "答疑范围待核实"))
    elif device.get("mode"):
        messages.append("执行器尚未回报实际答疑范围和操作额度")
    return {"scope": messages, "policy_reported": bool(policy),
            "next_eligible_at": device.get("participation_next_at"),
            "reason": device.get("participation_wait_reason")}


def report(c, agent, day, now):
    config = discovery_config(c, agent)
    if not config.enabled:
        return None
    args = {"aid": agent["id"], "day": day, "now": now}
    receipts = all_rows(c, METRICS, args)
    metrics = {row["metric"]: {"count": row["total"], "records": row["records"]}
               for row in receipts if row["metric"]}
    work = all_rows(c, """
        SELECT type,state,input#>>'{candidate,name}' AS name,result->>'reply_state' AS reply_state,
          result->>'join_state' AS join_state, result#>>'{outcome,reason}' AS reason,
          result->>'phase' AS phase, result->'native_cursor' AS native_cursor,
          CASE WHEN jsonb_typeof(input->'native_queries')='array' THEN jsonb_array_length(input->'native_queries') END AS query_count,
          CASE WHEN jsonb_typeof(result->'sources')='array' THEN jsonb_array_length(result->'sources') END AS source_count,
          result#>>'{checkpoint,at}' AS checkpoint_at
        FROM lead.tasks WHERE agent_id=%s AND type IN ('community.engage','community.discover')
          AND state IN ('running','reconciling','queued','planned','waiting_user')
        ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'reconciling' THEN 1 ELSE 2 END,
          scheduled_at NULLS LAST,created_at DESC,id
    """, (agent["id"],))
    active = next((r for r in work if r["state"] == "running"), None)
    queued = next((r for r in work if r["state"] in ("planned", "queued")), None)
    paused = agent["desired_state"] != "running"
    current = "运营未启动或已暂停" if paused else task_action(active) if active else "当前没有在执行的社群任务"
    next_action = task_action(queued) if queued else None
    if not next_action and not paused:
        decision = one(c, """SELECT result#>>'{selected,reason}' AS reason FROM lead.tasks
            WHERE agent_id=%s AND type='operating.decision' AND state IN ('planned','queued','running')
              AND result#>>'{selected,kind}' IN ('community_progress','community_discovery')
              AND input->>'config_revision'=%s ORDER BY created_at DESC LIMIT 1""",
            (agent["id"],str(agent["config_revision"])))
        next_action = decision["reason"] if decision else None
    latest = one(c, """
        SELECT coalesce(input#>>'{candidate,name}','Telegram') AS name,
          result#>>'{outcome,reason}' AS reason FROM lead.tasks WHERE agent_id=%(aid)s
          AND type='community.engage' AND completed_at BETWEEN %(day)s AND %(now)s
          AND result#>>'{outcome,state}' IN ('skipped','blocked','pending_verification','unknown')
          AND result#>>'{outcome,reason}' IS NOT NULL ORDER BY completed_at DESC,id LIMIT 1
    """, args)
    owner = next((r["reason"] or task_action(r) for r in work if r["state"] == "waiting_user"), None)
    accounts = one(c, """SELECT count(*) FILTER (WHERE state='connected' AND automation_enabled) AS ready,
        count(*) FILTER (WHERE state='needs_auth') AS needs_auth FROM lead.social_accounts
        WHERE agent_id=%s AND platform='telegram'""", (agent["id"],))
    if not owner and not accounts["ready"]:
        owner = "请重新登录 Telegram 账号。" if accounts["needs_auth"] else "请连接 Telegram 账号并启用运营。"
    if not owner and config.mode == "discovery" and metrics.get("candidate"):
        owner = "当前仅允许找群；如需入群，请授权社群参与。"
    visits = one(c, """WITH posted AS (
          SELECT DISTINCT r.input#>>'{reply,tracking_link_id}' AS link FROM lead.tasks r
          WHERE r.agent_id=%(aid)s AND r.type='community.engage' AND r.result->>'reply_state'='confirmed'
          AND r.input#>>'{reply,promote}'='true'
          AND coalesce((r.result->>'reply_dispatched_at')::timestamptz,r.completed_at) BETWEEN %(day)s AND %(now)s
        ) SELECT count(*) AS visits FROM lead.visit_sessions v WHERE v.agent_id=%(aid)s
          AND v.started_at BETWEEN %(day)s AND %(now)s AND v.source='redirect'
          AND v.exclusion_reason IS NULL AND v.tracking_link_id::text IN (SELECT link FROM posted)
        """, args)
    business = agent["onboarding_state"].get("community_progress", {})
    scopes = business.get("blocked_scopes", [])
    if not owner and any(scope.get("error_code") == "authentication_required" for scope in scopes):
        owner = "受影响的 Telegram 账号需要重新登录，完成后系统会只读复查。"
    autonomy = {"last_progress_at": business.get("last_progress_at"),
                "progress_label": {"qualified_candidate": "新增合格候选", "membership_confirmed": "确认加入群", "message_confirmed": "取得发言回执", "filtered_click": "获得有效点击"}.get(business.get("progress_kind"), "暂无已验证进展"),
                "blocked_targets": sum(s.get("peer") != "*" for s in scopes),
                "blocked_accounts": sum(s.get("peer") == "*" for s in scopes),
                "next_check_at": min((s["read_after"] for s in scopes if s.get("read_after")), default=None)}
    # A device can finish a discovery without refreshing the generic progress snapshot.
    # Keep first admission across day boundaries, even when today's count is zero.
    admitted = receipts[0]["admitted_at"]
    previous = clock_value(autonomy["last_progress_at"])
    if admitted and (not previous or admitted > previous):
        autonomy.update(last_progress_at=admitted, progress_label="新增合格候选")
    resident = resident_activity(agent, work, now)
    if resident:
        current = resident["current"]
        next_action = resident["next"] or next_action
        if accounts["ready"] and resident["owner"]:
            owner = resident["owner"] if not owner else f"{owner} {resident['owner']}"
    return {"participation": participation_status(agent, config), "resident": resident, "autonomy": autonomy, "execution": execution(c, agent["id"], day, now), "progress": progress(c, agent["id"], now), "promotion_visits": visits["visits"], "metrics": metrics, "at": now, "current": current,
            "next": ("恢复运营后重新排期" if paused else next_action or "暂无已排定的下一项社群行动"),
            "constraint": f"「{latest['name']}」{latest['reason']}" if latest else None,
            "owner": owner}
