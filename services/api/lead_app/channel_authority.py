"""Driver eligibility and exact account grants remain separate checks."""

from .db import all_rows, one, require_agent, tx


def has_driver(agent: dict, platform: str) -> bool:
    return (
        agent["onboarding_state"].get("managed_channel_delivery", {}).get(platform)
        is True
    )


def managed_accounts(c, aid: str, platform: str) -> list[dict]:
    from .channels import managed_scope

    return [
        row
        for row in all_rows(
            c,
            """SELECT * FROM lead.social_accounts
        WHERE agent_id=%s AND platform=%s AND state='connected' AND automation_enabled ORDER BY created_at,id""",
            (aid, platform),
        )
        if managed_scope(c, row)
    ]


def driver_accounts(c, aid, platform):
    from .atomic_dispatch import channel_pilot
    from .reddit_member import member_scope

    agent = require_agent(c, aid)
    if has_driver(agent, platform):
        accounts = {str(row["id"]): row for row in managed_accounts(c, aid, platform)}
        if platform == "reddit" and agent["onboarding_state"].get(
            "reddit_member_delivery"
        ):
            for row in all_rows(
                c,
                "SELECT * FROM lead.social_accounts WHERE agent_id=%s AND platform=%s AND state='connected' AND automation_enabled",
                (aid, platform),
            ):
                if member_scope(c, row):
                    accounts[str(row["id"])] = row
        return list(accounts.values())
    rows = all_rows(
        c,
        "SELECT * FROM lead.social_accounts WHERE agent_id=%s AND platform=%s AND state='connected' AND automation_enabled ORDER BY created_at,id",
        (aid, platform),
    )
    if channel_pilot(aid):
        return rows
    if platform == "reddit" and agent["onboarding_state"].get("reddit_member_delivery"):
        return [row for row in rows if member_scope(c, row)]
    return []


def binding_snapshot(accounts):
    return sorted(
        (
            {
                "account_id": str(a["id"]),
                "binding_version": a["binding_version"],
                "external_account_id": a["external_account_id"],
            }
            for a in accounts
        ),
        key=lambda item: item["account_id"],
    )


def authorize_managed_scope(aid, account_id, binding_version, expected_control_version):
    from .channels import authorize_managed_discovery
    from .domain import channel_control

    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE agent_id=%s AND id=%s FOR UPDATE",
            (aid, account_id),
        )
        if (
            not account
            or agent["control_version"] != expected_control_version
            or account["binding_version"] != binding_version
        ):
            raise ValueError("项目或账号绑定已变化，请重新核对后启动")
        authorize_managed_discovery(
            c,
            aid,
            account_id,
            account["platform"],
            account["external_account_id"],
            binding_version,
            explicit=True,
        )
        channel_control(
            aid, account["platform"], True, connection=c, start_account=account_id
        )
    return {"ok": True, "account_id": str(account_id)}


def research_binding_valid(c, aid, payload):
    rows = driver_accounts(c, aid, payload["platform"])
    if payload.get("channel_account_id"):
        return any(
            str(row["id"]) == payload["channel_account_id"]
            and row["binding_version"] == payload.get("channel_account_binding")
            and str(row["profile_id"]) == payload.get("channel_profile_id")
            for row in rows
        )
    # Old pilot histories may only contain a profile; explicit managed work
    # requires the new exact binding capture before making another browser read.
    if has_driver(require_agent(c, aid), payload["platform"]):
        return False
    return any(
        str(row["profile_id"]) == payload.get("channel_profile_id") for row in rows
    )
