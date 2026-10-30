"""In-app notifications from real state, with episode deduplication and receipts."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .admin_operations import work_window
from .admin_settings import effective
from .db import all_rows, j, one, tx
from .scope import actor_ref, principal


def available_at(settings, severity, now):
    hours = settings["quiet_hours"]
    if not hours["enabled"] or (
        severity == "error" and settings["urgent_during_quiet"]
    ):
        return now
    if not work_window(hours, now)["allowed"]:
        return now
    # The notification becomes visible at the first instant outside quiet hours.
    for minute in range(1, 24 * 60 + 2):
        candidate = now + timedelta(minutes=minute)
        if not work_window(hours, candidate)["allowed"]:
            return candidate.replace(second=0, microsecond=0)
    return now + timedelta(days=1)


def condition(
    c,
    key,
    category,
    active,
    *,
    aid=None,
    title="",
    message="",
    severity="warning",
    settings=None,
    now=None,
):
    now = now or datetime.now(timezone.utc)
    settings = settings or effective(c, "notifications", aid)[0]
    c.execute(
        "INSERT INTO lead.admin_notification_conditions(tenant_id,condition_key) VALUES(lead.ctx_tenant(),%s) ON CONFLICT DO NOTHING",
        (key,),
    )
    old = one(
        c,
        "SELECT * FROM lead.admin_notification_conditions WHERE condition_key=%s FOR UPDATE",
        (key,),
    )
    should_notify = (
        active and settings["enabled"] and category in settings["categories"]
    )
    if old["active"] and not should_notify:
        c.execute(
            "UPDATE lead.admin_notifications SET resolved_at=%s WHERE condition_key=%s AND resolved_at IS NULL",
            (now, key),
        )
    if should_notify and not old["active"]:
        episode = old["episode"] + 1
        due = available_at(settings, severity, now)
        if old["last_notified_at"]:
            due = max(
                due,
                old["last_notified_at"]
                + timedelta(minutes=settings["recurrence_cooldown_minutes"]),
            )
        notification = one(
            c,
            """INSERT INTO lead.admin_notifications(tenant_id,agent_id,condition_key,episode,category,severity,title,message,recipient_roles,recipient_subjects,created_at,available_at)
            VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                aid,
                key,
                episode,
                category,
                severity,
                title,
                message,
                j(settings["recipient_roles"]),
                j(settings["recipient_subjects"]),
                now,
                due,
            ),
        )
        from .admin_delivery import enqueue

        enqueue(c, notification)
        c.execute(
            "UPDATE lead.admin_notification_conditions SET active=true,episode=%s,last_notified_at=%s WHERE condition_key=%s",
            (episode, due, key),
        )
    elif old["active"] != should_notify:
        c.execute(
            "UPDATE lead.admin_notification_conditions SET active=%s WHERE condition_key=%s",
            (should_notify, key),
        )


def collect(now=None):
    """Called by the workspace worker and inbox reads; no external messages."""
    from .budget import configuration

    now = now or datetime.now(timezone.utc)
    with tx() as c:
        c.execute(
            "UPDATE lead.admin_notifications SET resolved_at=%s WHERE category IN ('budget_warning','budget_exhausted') AND created_at<%s AND resolved_at IS NULL",
            (now, now.replace(hour=0, minute=0, second=0, microsecond=0)),
        )
        agents = all_rows(
            c,
            """SELECT a.id,a.name,a.health_state,a.desired_state,
            coalesce(b.spent_units,0)+coalesce(b.reserved_units,0) AS occupied,
            EXISTS(SELECT 1 FROM lead.social_accounts s WHERE s.agent_id=a.id AND s.state='needs_auth') AS needs_auth,
            EXISTS(SELECT 1 FROM lead.actions x WHERE x.agent_id=a.id AND x.state IN ('dispatching','unknown')) AS uncertain,
            coalesce((SELECT max(failures) FROM lead.admin_runtime_state r WHERE r.agent_id=a.id),0) AS failures
            FROM lead.agents a LEFT JOIN LATERAL(SELECT * FROM lead.budget_periods b WHERE b.agent_id=a.id AND b.unit='tokens'
            AND b.starts_at<=%s AND b.ends_at>%s LIMIT 1) b ON true
            WHERE a.desired_state<>'archived'""",
            (now, now),
        )
        for agent in agents:
            aid = str(agent["id"])
            settings, _ = effective(c, "notifications", aid)
            limit = configuration(c, aid)["daily_token_limit"]
            runtime, _ = effective(c, "runtime", aid)
            brand, _ = effective(c, "brand", aid)
            from .admin_controls import attribution

            measurement, _ = attribution(c, aid)
            stale = one(
                c,
                """SELECT EXISTS(SELECT 1 FROM lead.analytics_connections WHERE agent_id=%s AND provider='lead_site' AND state='verified' AND last_received_at<%s)
              OR EXISTS(SELECT 1 FROM lead.analytics_credentials WHERE agent_id=%s AND state='active' AND last_received_at<%s) AS value""",
                (
                    aid,
                    now - timedelta(hours=measurement["freshness_hours"]),
                    aid,
                    now - timedelta(hours=measurement["freshness_hours"]),
                ),
            )["value"]
            expired = any(
                doc["status"] == "approved"
                and doc["valid_until"]
                and doc["valid_until"]
                < now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
                for doc in brand["documents"]
            )
            observations = [
                (
                    "measurement_stale",
                    stale,
                    "网站或转化回传已过期",
                    "已超过配置的数据新鲜度窗口，请核对接入和回传服务。",
                    "warning",
                ),
                (
                    "budget_warning",
                    limit > 0
                    and settings["budget_warning_percent"] * limit
                    <= agent["occupied"] * 100
                    < limit * 100,
                    "每日额度接近上限",
                    "已用量与调用中预留已达到提醒比例，可查看额度分配。",
                    "warning",
                ),
                (
                    "budget_exhausted",
                    agent["occupied"] >= limit,
                    "今日可用额度已用尽",
                    "新增模型调用已受额度限制，已用量与待核对预留继续保留。",
                    "error",
                ),
                (
                    "login_expired",
                    agent["needs_auth"],
                    "渠道账号需要重新登录",
                    "请在工作台的连接入口完成验证。",
                    "warning",
                ),
                (
                    "repeated_failures",
                    agent["failures"] >= runtime["pause_after_failures"],
                    "连续失败需要处理",
                    "请查看异常记录。已自动暂停的 Agent 需要处理后手动恢复。",
                    "error",
                ),
                (
                    "uncertain_action",
                    agent["uncertain"],
                    "外部操作结果正在核对",
                    "系统会核对已有提交，请勿重复提交相同内容。",
                    "warning",
                ),
                (
                    "knowledge_expired",
                    expired,
                    "品牌知识资料已过期",
                    "过期资料已从模型上下文中排除，请更新来源或有效期。",
                    "warning",
                ),
            ]
            for category, active, title, message, severity in observations:
                period = (
                    ":" + now.date().isoformat()
                    if category.startswith("budget_")
                    else ""
                )
                condition(
                    c,
                    category + ":" + aid + period,
                    category,
                    bool(active),
                    aid=aid,
                    title=agent["name"] + " · " + title,
                    message=message,
                    severity=severity,
                    settings=settings,
                    now=now,
                )
        # Resolve conditions for archived Agents without touching active history.
        c.execute(
            """UPDATE lead.admin_notifications n SET resolved_at=%s FROM lead.agents a
            WHERE n.agent_id=a.id AND a.desired_state='archived' AND n.resolved_at IS NULL""",
            (now,),
        )


def visible(c, limit=50, before=None, before_id=None):
    who = principal() or {"role": "owner", "subject": "local"}
    return all_rows(
        c,
        """SELECT n.*,r.read_at FROM lead.admin_notifications n
        LEFT JOIN lead.admin_notification_receipts r ON r.notification_id=n.id AND r.actor_ref=%s
        WHERE n.available_at<=now() AND (n.recipient_roles ? %s OR n.recipient_subjects ? %s)
        AND (%s::timestamptz IS NULL OR (n.created_at,n.id)<(%s,%s::uuid))
        ORDER BY n.created_at DESC,n.id DESC LIMIT %s""",
        (
            actor_ref(),
            who["role"],
            who["subject"],
            before,
            before,
            before_id or "00000000-0000-0000-0000-000000000000",
            limit,
        ),
    )


def mark_read(c, notification_id):
    who = principal() or {"role": "owner", "subject": "local"}
    row = one(
        c,
        "SELECT id FROM lead.admin_notifications WHERE id=%s AND available_at<=now() AND (recipient_roles ? %s OR recipient_subjects ? %s)",
        (notification_id, who["role"], who["subject"]),
    )
    if not row:
        raise LookupError("通知不存在")
    c.execute(
        "INSERT INTO lead.admin_notification_receipts(tenant_id,notification_id,actor_ref) VALUES(lead.ctx_tenant(),%s,%s) ON CONFLICT DO NOTHING",
        (notification_id, actor_ref()),
    )
    return {"ok": True}
