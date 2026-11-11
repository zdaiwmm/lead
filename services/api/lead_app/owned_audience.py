"""A business post needs an audience beyond the publishing account."""

from datetime import datetime, timedelta, timezone

import httpx

from .db import j, one, tx


def status(observation, now):
    if observation and observation.get("retry_at"):
        if datetime.fromisoformat(observation["retry_at"]) <= now:
            return "due"
        count = observation.get("count")
        return "eligible" if count is not None and count > 1 else "unknown"
    if (
        not observation
        or datetime.fromisoformat(observation["checked_at"]) + timedelta(hours=6) <= now
    ):
        return "due"
    count = observation.get("count")
    if count is None:
        return "unknown"
    return "eligible" if count > 1 else "empty"


def save(c, target_id, count):
    now = datetime.now(timezone.utc)
    old = one(c, "SELECT verified_permissions->'owned_audience' AS value FROM lead.account_targets WHERE id=%s", (target_id,))
    if count is None:
        observation = {**((old or {}).get("value") or {}), "failed_at": now.isoformat(),
                       "retry_at": (now + timedelta(minutes=1)).isoformat()}
    else:
        observation = {"count": count, "checked_at": now.isoformat()}
    c.execute(
        "UPDATE lead.account_targets SET verified_permissions=verified_permissions || %s WHERE id=%s",
        (j({"owned_audience": observation}), target_id),
    )


async def inspect(aid, target_id):
    from .activities import browser_rpc

    with tx(aid) as c:
        target = one(
            c,
            "SELECT account_id,canonical_url,external_target_id FROM lead.account_targets WHERE id=%s AND state='active'",
            (target_id,),
        )
    if not target:
        return {"delay": 21600, "reason": "target_unavailable"}

    count = None
    try:
        observed = await browser_rpc(
            f"/agents/{aid}/telegram/target",
            {"account_id": str(target["account_id"]), "url": target["canonical_url"]},
        )
        if observed["external_target_id"] == target["external_target_id"]:
            count = observed.get("audience_count")
    except httpx.HTTPError:
        pass  # Record an unavailable observation and yield to independent work.
    with tx(aid) as c:
        save(c, target_id, count)
    reason = "频道人数暂不可见，先做其他获客工作"
    if count is not None:
        reason = (
            "频道只有运营账号或无人订阅，先做外部获客"
            if count <= 1
            else "频道已有其他订阅，可安排分发"
        )
    return {"delay": 60 if count is None else 21600, "reason": reason, "audience_count": count}
