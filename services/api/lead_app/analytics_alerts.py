"""Live Agent health episodes reuse the notification center and its delivery policy."""

from .admin_notifications import available_at, condition
from .admin_settings import effective
from .analytics_ingest import scoped_tx
from .analytics_limits import policy
from .db import j, one, require_agent, tx
from .scope import current_tenant


def collect_agent(agent, now):
    with scoped_tx(current_tenant(), agent, "live") as c:
        require_agent(c, agent)
        cfg, _ = policy(c)
        settings, _ = effective(c, "notifications", agent)
        state = one(
            c,
            """SELECT
          EXISTS(SELECT 1 FROM lead.analytics_ingest_events WHERE state='pending' AND received_at<%s-make_interval(secs=>%s)) AS backlog,
          EXISTS(SELECT 1 FROM lead.outbox_events o JOIN lead.analytics_ingest_events e ON e.outbox_id=o.id
            WHERE (e.state='pending' AND o.delivery_error IS NOT NULL AND o.delivery_attempts>=%s) OR e.state='failed') AS delivery,
          EXISTS(SELECT 1 FROM lead.analytics_report_dirty WHERE state='failed') AS report,
          EXISTS(SELECT 1 FROM lead.analytics_limit_counters WHERE environment='production' AND last_denied_at>%s-interval '5 minutes') AS limited""",
            (now, cfg.backlog_seconds, cfg.delivery_failure_threshold, now),
        )
    # Condition heads are workspace records. The observations above were read
    # under the required Agent scope; notification rows retain their Agent ID.
    with tx() as c:
        rules = [
            (
                "analytics_ingest_backlog",
                state["backlog"],
                "数据处理积压",
                "采集事件等待时间已超过设置阈值。",
            ),
            (
                "analytics_delivery_failure",
                state["delivery"],
                "数据投递或处理失败",
                "事件已保存，请核对数据状态和处理服务，失败事件可在保留期内重试。",
            ),
            (
                "analytics_report_failure",
                state["report"],
                "统计更新失败",
                "部分日期尚未更新，可在数据状态中重建。",
            ),
            (
                "analytics_rate_limited",
                state["limited"],
                "采集达到保护上限",
                "部分上报需要重试，部分短链点击未采集，请核对当前 Agent 的采集设置。",
            ),
        ]
        for category, active, title, message in rules:
            key = f"{category}:{agent}:live"
            old = one(
                c,
                "SELECT * FROM lead.admin_notification_conditions WHERE condition_key=%s FOR UPDATE",
                (key,),
            )
            condition(
                c,
                key,
                category,
                bool(active),
                aid=agent,
                title=title,
                message=message,
                settings=settings,
                now=now,
            )
            # Disabling notifications is not evidence that service recovered.
            if (
                old
                and old["active"]
                and not active
                and settings["enabled"]
                and category in settings["categories"]
                and "analytics_recovered" in settings["categories"]
            ):
                row = one(
                    c,
                    """INSERT INTO lead.admin_notifications(tenant_id,agent_id,condition_key,episode,category,severity,title,message,recipient_roles,recipient_subjects,created_at,available_at)
                  VALUES(lead.ctx_tenant(),%s,%s,%s,'analytics_recovered','info',%s,%s,%s,%s,%s,%s)
                  ON CONFLICT(tenant_id,condition_key,episode) DO NOTHING RETURNING *""",
                    (
                        agent,
                        key + ":recovery",
                        old["episode"],
                        title + "已恢复",
                        "当前 Agent 的这项异常已解除。",
                        j(settings["recipient_roles"]),
                        j(settings["recipient_subjects"]),
                        now,
                        available_at(settings, "info", now),
                    ),
                )
                if row:
                    from .admin_delivery import enqueue

                    enqueue(c, row)
