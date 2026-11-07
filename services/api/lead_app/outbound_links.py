"""Publishing boundary for the separately owned shortlink collector."""

import re
from ipaddress import ip_address
from urllib.parse import urlsplit

from .db import one


def external_url_problem(value, *, https=False):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ({"https"} if https else {"http", "https"}):
            return True
        if not host or parsed.username or parsed.password:
            return True
        if host == "localhost" or host.endswith(
            (".localhost", ".local", ".localdomain", ".internal")
        ):
            return True
        try:
            address = ip_address(host)
            return not address.is_global or address.is_multicast
        except ValueError:
            return "." not in host
    except (TypeError, ValueError):
        return True


def configured_problem():
    from . import promotion
    from .analytics import public_origin

    # Older installations publish a direct business URL. Detect the delivered
    # shortlink implementation without replacing or duplicating its collector.
    if not callable(getattr(promotion, "tracking_url", None)):
        return None
    try:
        invalid = external_url_problem(public_origin(), https=True)
    except ValueError:
        invalid = True
    return "public_shortlink_required" if invalid else None


def guard_action(c, action):
    if action["intent"].get("platform") == "lab":
        return
    task = one(c, "SELECT input FROM lead.tasks WHERE id=%s", (action["task_id"],))
    data = task["input"]
    if data.get("bootstrap_verification") or not data.get("link"):
        return
    if external_url_problem(data["link"]):
        raise ValueError("推广链接仍是本机或内网地址，已保留原稿，等待平台公网短链配置")


def reply_link_problem(publication):
    if not publication.get("promote"):
        return False
    return any(
        external_url_problem(url)
        for url in re.findall(r"https?://[^\s<>，。；）)\]]+", publication["body"])
    )
