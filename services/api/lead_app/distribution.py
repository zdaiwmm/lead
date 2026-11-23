"""One durable channel/placement per intent, shared by publication and measurement."""

import base64
import hmac
import json
import secrets
from urllib.parse import urlsplit

from .db import one
from .promotion import PLATFORM_CODES, new_marker
from .scope import current_tenant


def registration_marker(aid, platform, intent, placement, *, compact=True):
    from .chamber_analytics import production_bridge_enabled

    if not production_bridge_enabled(aid):
        return new_marker(platform) if compact else secrets.token_urlsafe(12)
    from .config import CONFIG

    identity = json.dumps([current_tenant(), str(aid), platform, intent, placement])
    digest = hmac.digest(
        CONFIG["session_secret"].encode(), ("chamber-link-v1:" + identity).encode(), "sha256"
    )
    marker = base64.urlsafe_b64encode(digest[:8 if compact else 12]).decode().rstrip("=")
    return PLATFORM_CODES[platform] + "_" + marker if compact else marker


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
        (
            *key,
            *source,
            code or registration_marker(aid, platform, intent, placement),
            urlsplit(destination).hostname,
        ),
    )
    if not row:
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
        row = {"id": old["id"], "public_code": old["public_code"]}
    if placement != "chamber" and platform != "lab":
        from .chamber_analytics import register_production_link

        register_production_link(aid, row["public_code"], platform, destination)
    return row
