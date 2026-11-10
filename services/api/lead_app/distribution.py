"""One durable channel/placement per intent, shared by publication and measurement."""

from urllib.parse import urlsplit

from .db import one
from .promotion import new_marker
from .scope import current_tenant


def register(
    c,
    aid,
    platform,
    destination,
    *,
    intent,
    placement,
    account=None,
    location=None,
    campaign=None,
    content=None,
    code=None,
):
    if account and not one(
        c,
        "SELECT id FROM lead.social_accounts WHERE id=%s AND platform=%s",
        (account, platform),
    ):
        raise ValueError("分发账号与当前渠道不匹配")
    key = (current_tenant(), aid, platform, intent, placement)
    source = (
        destination,
        str(account) if account else None,
        location,
        campaign,
        str(content) if content else None,
    )
    row = one(
        c,
        """INSERT INTO lead.tracking_links
        (tenant_id,agent_id,platform,intent_key,placement,destination_url,account_id,placement_ref,campaign,content_id,public_code,allowed_host)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(tenant_id,agent_id,platform,intent_key,placement) DO NOTHING RETURNING id,public_code""",
        (*key, *source, code or new_marker(platform), urlsplit(destination).hostname),
    )
    if row:
        return row
    old = one(
        c,
        """SELECT id,public_code,destination_url,account_id::text,placement_ref,campaign,content_id::text,state
        FROM lead.tracking_links WHERE tenant_id=%s AND agent_id=%s AND platform=%s AND intent_key=%s AND placement=%s""",
        key,
    )
    if (
        not old
        or old["state"] != "active"
        or tuple(
            old[k]
            for k in (
                "destination_url",
                "account_id",
                "placement_ref",
                "campaign",
                "content_id",
            )
        )
        != source
    ):
        raise ValueError("该分发位置已登记，请为新的目的地或内容另建版本")
    return {"id": old["id"], "public_code": old["public_code"]}
