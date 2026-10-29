from datetime import datetime, timedelta, timezone

from .config import env
from .db import one, tx
from .scope import current_tenant


class BudgetExceeded(Exception):
    pass


def reserve(aid, operation_key, estimate):
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    limit = int(env().get("LEAD_DAILY_TOKEN_LIMIT") or 100000)
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.budget_periods(tenant_id,agent_id,starts_at,ends_at,unit,limit_units) VALUES(%s,%s,%s,%s,'tokens',%s) ON CONFLICT(tenant_id,agent_id,starts_at,unit) DO NOTHING",
            (current_tenant(), aid, start, start + timedelta(days=1), limit),
        )
        period = one(
            c,
            "SELECT * FROM lead.budget_periods WHERE starts_at=%s AND unit='tokens' FOR UPDATE",
            (start,),
        )
        if (
            period["spent_units"] + period["reserved_units"] + estimate
            > period["limit_units"]
        ):
            raise BudgetExceeded("daily_token_limit")
        reservation = one(
            c,
            "INSERT INTO lead.budget_reservations(tenant_id,agent_id,period_id,operation_key,units,state,expires_at) VALUES(%s,%s,%s,%s,%s,'reserved',%s) RETURNING id",
            (
                current_tenant(),
                aid,
                period["id"],
                operation_key,
                estimate,
                start + timedelta(days=1),
            ),
        )
        c.execute(
            "UPDATE lead.budget_periods SET reserved_units=reserved_units+%s WHERE id=%s",
            (estimate, period["id"]),
        )
        return str(reservation["id"])


def settle(aid, reservation_id, actual=None, known_unbilled=False):
    with tx(aid) as c:
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
            ("settled" if actual is not None else "released", reservation_id),
        )
        period = one(
            c,
            "UPDATE lead.budget_periods SET reserved_units=reserved_units-%s,spent_units=spent_units+%s WHERE id=%s RETURNING spent_units,limit_units",
            (r["units"], actual or 0, r["period_id"]),
        )
        if actual is not None:
            c.execute(
                "INSERT INTO lead.usage_events(tenant_id,agent_id,reservation_id,event_key,category,units,rate_version,occurred_at) VALUES(%s,%s,%s,%s,'model_tokens',%s,'provider_usage_v1',now()) ON CONFLICT DO NOTHING",
                (current_tenant(), aid, reservation_id, reservation_id, actual),
            )
        if period["spent_units"] > period["limit_units"]:
            c.execute(
                "UPDATE lead.agents SET desired_state='paused',health_state='needs_attention',control_version=control_version+1 WHERE id=%s",
                (aid,),
            )
