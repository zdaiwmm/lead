"""Signed per-click evidence. Tokens are attribution proofs, never API credentials."""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException

from .config import CONFIG
from .db import one


def _signature(value):
    return hmac.new(
        CONFIG["session_secret"].encode(),
        ("analytics-click-v1:" + value).encode(),
        hashlib.sha256,
    ).hexdigest()


def mint(click):
    data = {k: str(click[k]) for k in ("tenant_id", "agent_id", "id")}
    data["exp"] = int(click["expires_at"].timestamp())
    value = (
        base64.urlsafe_b64encode(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    return "lc1." + value + "." + _signature(value)


def resolve(c, conn, body, test, *, allow_expired=False):
    token, ident = body.click_token, body.click_id
    if not token and not ident:
        return None
    try:
        if test:
            raise ValueError()
        if token:
            if len(token) > 1024:
                raise ValueError()
            version, value, signature = token.split(".")
            if version != "lc1" or not hmac.compare_digest(
                signature, _signature(value)
            ):
                raise ValueError()
            data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
            if data["tenant_id"] != str(conn["tenant_id"]) or data["agent_id"] != str(
                conn["agent_id"]
            ):
                raise ValueError()
            click_id = UUID(data["id"])
            if (ident and ident != click_id) or (
                not allow_expired
                and data["exp"] < datetime.now(timezone.utc).timestamp()
            ):
                raise ValueError()
        else:
            # A server-persisted touch is already validated click evidence. This
            # keeps cross-tab snapshots usable without treating a bare click ID
            # as proof or borrowing another connection/visitor's touch.
            touch = one(
                c,
                """SELECT click_id FROM lead.pixel_touchpoints WHERE id=%s AND connection_id=%s AND click_id=%s
              AND (%s::uuid IS NULL OR anonymous_id=%s)""",
                (
                    body.touchpoint_id,
                    conn["id"],
                    ident,
                    body.anonymous_id,
                    body.anonymous_id,
                ),
            )
            if not touch:
                raise ValueError()
            click_id = ident
        # The caller has already selected the live Agent scope.
        click = one(
            c,
            "SELECT * FROM lead.analytics_clicks WHERE id=%s AND (%s OR expires_at>now())",
            (click_id, allow_expired),
        )
        from urllib.parse import urlsplit

        if (
            not click
            or click["destination_host"] != urlsplit(conn["site_host"]).hostname
        ):
            raise ValueError()
        body.click_id = click_id
        return click
    except (ValueError, KeyError, TypeError):
        raise HTTPException(
            422,
            {"code": "CLICK_PROOF_INVALID", "message": "点击凭证无效或不属于此 Agent"},
        ) from None


def eligible(click, occurred):
    from datetime import timedelta

    return click and datetime.fromisoformat(
        str(click["occurred_at"])
    ) <= occurred <= datetime.fromisoformat(str(click["occurred_at"])) + timedelta(
        days=click["attribution_policy"]["attribution_window_days"]
    )
