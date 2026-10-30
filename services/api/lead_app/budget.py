from datetime import datetime, timedelta, timezone

from .config import env
from .db import all_rows, audit, emit, j, one, require_agent, tx
from .scope import current_tenant


class BudgetExceeded(Exception):
    pass


DEFAULT_DAILY_TOKEN_LIMIT = 1_000_000_000
MAX_DAILY_TOKEN_LIMIT = 9_007_199_254_740_991


def configuration(c, aid):
    agent = require_agent(c, aid)
    from .admin_config import head, published

    configured = published(c, "budgets/default")
    default = (
        configured["daily_token_limit"]
        if configured
        else int(env().get("LEAD_DAILY_TOKEN_LIMIT") or DEFAULT_DAILY_TOKEN_LIMIT)
    )
    custom = agent["onboarding_state"].get("daily_token_limit")
    config = head(c, "budgets/" + str(aid))
    return {
        "daily_token_limit": custom if custom is not None else default,
        "default_daily_token_limit": default,
        "custom_daily_token_limit": custom,
        "resets": "00:00 UTC",
        "config_version": config["version"] if config else 0,
    }


def get_configuration(aid):
    with tx(aid) as c:
        return configuration(c, aid)


def set_daily_limit(aid, value, expected_version=None):
    if value is not None and (
        type(value) is not int or not 0 <= value <= MAX_DAILY_TOKEN_LIMIT
    ):
        raise ValueError("每日 token 上限必须是有效的非负整数")
    from .admin_config import read, write

    with tx(aid) as c:
        require_agent(c, aid, True)
        key = "budgets/" + aid
        write(
            c,
            key,
            {"daily_token_limit": value},
            read(c, key)["version"] if expected_version is None else expected_version,
        )
        return configuration(c, aid)


def apply_daily_limit(c, aid, value):
    require_agent(c, aid, True)
    if value is None:
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state-'daily_token_limit',version=version+1 WHERE id=%s",
            (aid,),
        )
    else:
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s,version=version+1 WHERE id=%s",
            (j({"daily_token_limit": value}), aid),
        )
    config = configuration(c, aid)
    # Limit edits take effect today; actual usage and uncertain reservations stay.
    c.execute(
        "UPDATE lead.budget_periods SET limit_units=%s WHERE agent_id=%s AND unit='tokens' AND starts_at<=now() AND ends_at>now()",
        (config["daily_token_limit"], aid),
    )
    for job in all_rows(
        c,
        "UPDATE lead.chat_jobs SET status='pending',error_code=NULL WHERE agent_id=%s AND status IN ('blocked','failed') AND error_code='BUDGET_EXCEEDED' RETURNING id,message_id",
        (aid,),
    ):
        c.execute(
            "UPDATE lead.conversation_messages SET status='pending' WHERE id=%s",
            (job["message_id"],),
        )
        emit(c, aid, "chat.enqueue", {"job_id": str(job["id"])})
    audit(c, aid, "budget.daily_limit_changed", config)
    emit(c, aid, "supervisor.wake")
    return config


def reserve(aid, operation_key, estimate, model_ref=None, provider=None):
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with tx(aid) as c:
        require_agent(c, aid, True)
        limit = configuration(c, aid)["daily_token_limit"]
        c.execute(
            "INSERT INTO lead.budget_periods(tenant_id,agent_id,starts_at,ends_at,unit,limit_units) VALUES(%s,%s,%s,%s,'tokens',%s) ON CONFLICT(tenant_id,agent_id,starts_at,unit) DO NOTHING",
            (current_tenant(), aid, start, start + timedelta(days=1), limit),
        )
        period = one(
            c,
            "SELECT * FROM lead.budget_periods WHERE starts_at=%s AND unit='tokens' FOR UPDATE",
            (start,),
        )
        if period["limit_units"] != limit:
            c.execute(
                "UPDATE lead.budget_periods SET limit_units=%s WHERE id=%s",
                (limit, period["id"]),
            )
            period["limit_units"] = limit
        if (
            period["spent_units"] + period["reserved_units"] + estimate
            > period["limit_units"]
        ):
            raise BudgetExceeded("daily_token_limit")
        from .admin_pricing import quote

        reservation = one(
            c,
            "INSERT INTO lead.budget_reservations(tenant_id,agent_id,period_id,operation_key,units,state,expires_at,model_ref,provider,pricing_snapshot) VALUES(%s,%s,%s,%s,%s,'reserved',%s,%s,%s,%s) RETURNING id",
            (
                current_tenant(),
                aid,
                period["id"],
                operation_key,
                estimate,
                start + timedelta(days=1),
                model_ref,
                provider,
                j(quote(c, model_ref)),
            ),
        )
        c.execute(
            "UPDATE lead.budget_periods SET reserved_units=reserved_units+%s WHERE id=%s",
            (estimate, period["id"]),
        )
        return str(reservation["id"])


def settle(
    aid,
    reservation_id,
    actual=None,
    known_unbilled=False,
    details=None,
    incomplete=False,
):
    with tx(aid) as c:
        # All budget writers lock agent before period to prevent edit/admission/
        # overrun deadlocks. Billing still settles for an archived agent.
        one(c, "SELECT id FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,))
        r = one(
            c,
            "SELECT * FROM lead.budget_reservations WHERE id=%s FOR UPDATE",
            (reservation_id,),
        )
        if not r or r["state"] != "reserved":
            return
        if actual is None and not known_unbilled:
            c.execute(
                "UPDATE lead.budget_reservations SET state='unknown' WHERE id=%s",
                (reservation_id,),
            )
            return
        c.execute(
            "UPDATE lead.budget_reservations SET state=%s WHERE id=%s",
            (
                "unknown"
                if incomplete
                else "settled"
                if actual is not None
                else "released",
                reservation_id,
            ),
        )
        period = one(
            c,
            "UPDATE lead.budget_periods SET reserved_units=reserved_units-%s,spent_units=spent_units+%s WHERE id=%s RETURNING spent_units,limit_units",
            (0 if incomplete else r["units"], actual or 0, r["period_id"]),
        )
        if actual is not None:
            from .metering import priced

            measured = priced(details or {}, snapshot=r["pricing_snapshot"])
            c.execute(
                "INSERT INTO lead.usage_events(tenant_id,agent_id,reservation_id,event_key,category,units,rate_version,occurred_at,details) VALUES(%s,%s,%s,%s,'model_tokens',%s,%s,now(),%s) ON CONFLICT DO NOTHING",
                (
                    current_tenant(),
                    aid,
                    reservation_id,
                    reservation_id,
                    actual,
                    measured.get("rate_version") or "unpriced",
                    j(measured),
                ),
            )
        if period["spent_units"] > period["limit_units"]:
            c.execute(
                "UPDATE lead.agents SET desired_state='paused',health_state='needs_attention',control_version=control_version+1 WHERE id=%s",
                (aid,),
            )
