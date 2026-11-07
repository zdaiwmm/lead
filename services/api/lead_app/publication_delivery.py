"""A submission receipt is distinct from public distribution. No model calls."""

from datetime import datetime, timedelta, timezone

import httpx

from .config import STATE
from .db import all_rows, audit, emit, j, one, require_agent, tx
from .scope import service_scope, tenant_scope

STATES = {"unknown", "under_review", "private", "restricted", "rejected", "public"}
LABELS = {
    "unknown": "公开可见性尚待核对",
    "under_review": "平台审核中，尚未公开分发",
    "private": "仅自己可见，尚未公开分发",
    "restricted": "可见范围受限，尚不能作为公开分发",
    "rejected": "平台未通过该内容，原回执与证据保留",
    "public": "已核对公开可见",
}


def is_public(platform, delivery):
    if platform == "tiktok" or delivery:
        return (delivery or {}).get("state") == "public" and (
            platform != "tiktok" or delivery.get("visibility") == "public"
        )
    return True  # Existing adapters already require their native public receipt.


def signature(delivery):
    return tuple(
        (delivery or {}).get(k)
        for k in ("state", "visibility", "action_id", "external_url")
    )


def persist(c, aid, action_id, observed, evidence_ref=None, now=None):
    """Called after durable confirmation, before admitting another business plan."""
    now = now or datetime.now(timezone.utc)
    agent = require_agent(c, aid, True)
    row = one(
        c,
        """SELECT a.*,t.input AS task_input,t.result AS task_result,
        target.verified_permissions,s.platform,s.state AS account_state,s.automation_enabled,
        g.revoked_at FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id
        JOIN lead.account_targets target ON target.id=a.target_id
        JOIN lead.social_accounts s ON s.id=target.account_id
        JOIN lead.authorization_grants g ON g.id=a.grant_id
        WHERE a.id=%s AND a.state='confirmed'""",
        (action_id,),
    )
    if not row or row["platform"] != "tiktok":
        raise ValueError("交付状态必须关联本 Agent 已确认的原生回执")
    state = observed.get("state", "unknown")
    if state not in STATES:
        state = "unknown"
    if state == "public" and observed.get("visibility") != "public":
        state = "unknown"
    old = (row["task_result"] or {}).get("delivery", {})
    delivery = {
        "state": state,
        "visibility": observed.get("visibility", "unknown"),
        "publicly_visible": state == "public",
        "action_id": str(action_id),
        "external_url": row["external_url"],
        "checked_at": now.isoformat(),
        "next_check_at": (
            now + timedelta(minutes=5 if state == "under_review" else 15)
        ).isoformat(),
        "evidence_ref": evidence_ref or old.get("evidence_ref") or row["evidence_ref"],
    }
    if state == "public":
        delivery["public_at"] = (
            old.get("public_at") if old.get("state") == "public" else now.isoformat()
        )
    c.execute(
        "UPDATE lead.tasks SET result=coalesce(result,'{}'::jsonb) || %s WHERE id=%s",
        (j({"delivery": delivery}), row["task_id"]),
    )
    if row["task_input"].get("prepared_content_id"):
        c.execute(
            "UPDATE lead.tasks SET result=result || %s WHERE id=%s AND result->>'publication_task_id'=%s",
            (
                j({"status": "published" if state == "public" else "submitted"}),
                row["task_input"]["prepared_content_id"],
                str(row["task_id"]),
            ),
        )
    permissions = row["verified_permissions"]
    pending = one(
        c,
        """SELECT a.id,a.external_url,t.result->'delivery' AS delivery
        FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id WHERE a.target_id=%s
        AND a.state='confirmed' AND coalesce(t.result->'delivery'->>'state','unknown')<>'public'
        ORDER BY a.created_at LIMIT 1""",
        (row["target_id"],),
    )
    target_delivery = (
        (
            pending["delivery"]
            or {
                "state": "unknown",
                "visibility": "unknown",
                "action_id": str(pending["id"]),
                "external_url": pending["external_url"],
            }
        )
        if pending
        else delivery
    )
    updated = {**permissions, "publication_delivery": target_delivery}
    if pending:
        formats = sorted(
            set(permissions.get("pending_publishing_formats", []))
            | set(permissions.get("publishing_verified_formats", []))
        )
        if row["task_input"].get("bootstrap_verification"):
            formats = sorted(set(formats) | {row["intent"].get("format", "video")})
        updated.update(
            pending_publishing_formats=formats,
            publishing_verified_formats=[],
            publishing_verified=False,
        )
    elif (
        agent["desired_state"] == "running"
        and row["account_state"] == "connected"
        and row["automation_enabled"]
        and row["revoked_at"] is None
    ):
        if row["task_input"].get("bootstrap_verification"):
            one(c, "SELECT lead.confirm_channel_probe(%s) ok", (action_id,))
            refreshed = one(
                c,
                "SELECT verified_permissions FROM lead.account_targets WHERE id=%s",
                (row["target_id"],),
            )["verified_permissions"]
            updated.update(refreshed)
            updated["publication_delivery"] = delivery
        else:
            updated["publishing_verified_formats"] = permissions.get(
                "pending_publishing_formats",
                permissions.get("publishing_verified_formats", []),
            )
            updated["publishing_verified"] = bool(
                updated["publishing_verified_formats"]
            )
        if updated.get("publishing_verified_formats"):
            updated.pop("pending_publishing_formats", None)
    changed = signature(permissions.get("publication_delivery")) != signature(
        target_delivery
    )
    # Poll timestamps belong to the receipt, not the capability fingerprint.
    # An unchanged review must not wake research or mint a new plan revision.
    capability_changed = changed or updated.get(
        "publishing_verified_formats"
    ) != permissions.get("publishing_verified_formats")
    if capability_changed:
        c.execute(
            "UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s",
            (j(updated), row["target_id"]),
        )
    if signature(old) != signature(delivery) or capability_changed:
        audit(
            c,
            aid,
            "publication.delivery_observed",
            {
                "action_id": str(action_id),
                "state": state,
                "visibility": delivery["visibility"],
            },
        )
        emit(c, aid, "supervisor.wake")
    return delivery


def due():
    from .identity_store import execution_tenants

    rows = []
    with service_scope():
        for tenant in execution_tenants():
            with tenant_scope(tenant, None), tx() as c:
                rows.extend(
                    all_rows(
                        c,
                        """SELECT a.id,a.agent_id FROM lead.actions a
                    JOIN lead.tasks t ON t.id=a.task_id JOIN lead.agents agent ON agent.id=a.agent_id
                    JOIN lead.account_targets target ON target.id=a.target_id
                    JOIN lead.social_accounts s ON s.id=target.account_id
                    JOIN lead.authorization_grants g ON g.id=a.grant_id
                    WHERE a.state='confirmed' AND s.platform='tiktok' AND s.state='connected'
                    AND s.automation_enabled AND target.state='active' AND agent.desired_state='running'
                    AND g.revoked_at IS NULL
                    AND (coalesce(t.result->'delivery'->>'state','unknown')<>'public' OR target.verified_permissions ? 'pending_publishing_formats')
                    AND coalesce((t.result->'delivery'->>'next_check_at')::timestamptz,'-infinity')<=now()
                    ORDER BY a.created_at""",
                    )
                )
    return rows


async def review_actor(actor, action):
    """The confirmed branch of the ledger protocol can only read its old post."""
    from .adapters import get_adapter
    from .db import uid

    aid = actor.aid
    with tx(aid) as c:
        target = one(
            c,
            "SELECT t.*,s.external_account_id,s.platform,s.state AS account_state FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s",
            (action["target_id"],),
        )
        if (
            not target
            or target["platform"] != "tiktok"
            or target["account_state"] != "connected"
        ):
            raise ValueError("原账号暂不可核对，保留已确认回执")
        old = (
            one(c, "SELECT result FROM lead.tasks WHERE id=%s", (action["task_id"],))[
                "result"
            ]
            or {}
        ).get("delivery", {})
    page = await actor.context.new_page()
    try:
        observed = await get_adapter("tiktok").review_delivery(
            page,
            target,
            action["intent"],
            action["external_url"],
            target["external_account_id"],
        )
        path = None
        if any(observed.get(k) != old.get(k) for k in ("state", "visibility")):
            folder = STATE / "evidence" / aid
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (uid() + ".png")
            await page.screenshot(path=str(path))
            path.chmod(0o600)
        with tx(aid) as c:
            value = persist(
                c, aid, action["id"], observed, evidence_ref=str(path) if path else None
            )
        return {
            "state": "confirmed",
            "external_id": action["external_id"],
            "external_url": action["external_url"],
            "delivery": value,
        }
    finally:
        await page.close()


async def review_pending():
    from .security import internal_token

    for row in due():
        try:
            async with httpx.AsyncClient(timeout=110, trust_env=False) as client:
                result = await client.post(
                    "http://127.0.0.1:8089/agents/" + str(row["agent_id"]) + "/publish",
                    json={"action_id": str(row["id"])},
                    headers={"Authorization": "Bearer " + internal_token()},
                )
                result.raise_for_status()
        except Exception as error:
            # Keep the last observed state; a transport error cannot mean public.
            with service_scope(), tx(str(row["agent_id"])) as c:
                c.execute(
                    "UPDATE lead.tasks t SET result=jsonb_set(coalesce(t.result,'{}'),'{delivery}',coalesce(t.result->'delivery','{}') || %s) FROM lead.actions a WHERE a.task_id=t.id AND a.id=%s",
                    (
                        j(
                            {
                                "next_check_at": (
                                    datetime.now(timezone.utc) + timedelta(minutes=5)
                                ).isoformat(),
                                "check_error": type(error).__name__,
                            }
                        ),
                        row["id"],
                    ),
                )
