"""Small scoped fault records. User intent and uncertain write receipts stay intact."""

import logging
from datetime import datetime, timedelta, timezone

from .db import all_rows, one


def report(c, aid, scope, code, message, *, retry_seconds=60, needs_user=False):
    retry_at = (
        None
        if needs_user
        else datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)
    )
    c.execute(
        """INSERT INTO lead.runtime_issues(tenant_id,agent_id,scope,code,message,needs_user,retry_at)
      VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,%s)
      ON CONFLICT(tenant_id,agent_id,scope) DO UPDATE SET code=excluded.code,message=excluded.message,
      needs_user=excluded.needs_user,retry_at=excluded.retry_at,last_seen_at=now(),resolved_at=NULL,
      first_seen_at=CASE WHEN runtime_issues.resolved_at IS NOT NULL THEN now() ELSE runtime_issues.first_seen_at END,
      occurrences=CASE WHEN runtime_issues.resolved_at IS NOT NULL THEN 1 ELSE runtime_issues.occurrences+1 END
      WHERE runtime_issues.resolved_at IS NOT NULL OR runtime_issues.code<>excluded.code
        OR runtime_issues.message<>excluded.message OR runtime_issues.needs_user<>excluded.needs_user
        OR runtime_issues.last_seen_at<now()-interval '30 seconds'""",
        (aid, scope[:200], code[:100], message[:1000], needs_user, retry_at),
    )


def resolve(c, aid, scope):
    c.execute(
        "UPDATE lead.runtime_issues SET resolved_at=now(),retry_at=NULL WHERE agent_id=%s AND scope=%s AND resolved_at IS NULL",
        (aid, scope),
    )


def active(c, aid):
    rows = all_rows(
        c,
        "SELECT scope,code,message,needs_user,first_seen_at,last_seen_at,retry_at,occurrences FROM lead.runtime_issues WHERE agent_id=%s AND resolved_at IS NULL ORDER BY last_seen_at DESC LIMIT 40",
        (aid,),
    )
    labels = {
        "runtime:chat": "对话",
        "runtime:planning": "文案规划",
        "runtime:publish": "发布",
        "model_budget": "模型额度",
        "metrics": "统计数据",
        "review": "运营复盘",
        "plans": "运营计划",
        "progress": "进度汇总",
        "progress_history": "历史进度",
        "inbox": "收件箱",
        "followups": "后续工作安排",
        "notification_collection": "通知检查",
        "workflow_status": "任务汇总",
    }
    for row in rows:
        row["label"] = labels.get(
            row["scope"],
            "任务派发" if row["scope"].startswith("delivery:") else "浏览器验证" if row["scope"].startswith("browser:") else "局部工作",
        )
    return rows


def optional(c, aid, scope, fn, fallback=None):
    """Rollback a failed projection's SQL before continuing the core transaction."""
    waiting = one(
        c,
        "SELECT retry_at FROM lead.runtime_issues WHERE agent_id=%s AND scope=%s AND resolved_at IS NULL AND retry_at>now()",
        (aid, scope),
    )
    if waiting:
        return fallback
    try:
        with c.transaction():
            previous_timeout = one(
                c, "SELECT current_setting('statement_timeout') AS value"
            )["value"]
            c.execute("SELECT set_config('statement_timeout','1500ms',true)")
            value = fn()
            c.execute(
                "SELECT set_config('statement_timeout',%s,true)", (previous_timeout,)
            )
    except Exception as error:
        logging.warning(
            "Optional work unavailable: %s (%s)", scope, type(error).__name__
        )
        report(
            c,
            aid,
            scope,
            type(error).__name__,
            "这部分数据暂时无法更新，稍后自动重试。",
        )
        return fallback
    resolve(c, aid, scope)
    return value
