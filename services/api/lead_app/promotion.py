"""Compact, per-publication attribution shared by every promotion path."""

import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PLATFORM_CODES = {
    "telegram": "tg",
    "x": "x",
    "linkedin": "li",
    "facebook": "fb",
    "instagram": "ig",
    "youtube": "yt",
    "tiktok": "tt",
    "reddit": "rd",
    "pinterest": "pi",
    "threads": "th",
    "whatsapp": "wa",
    "direct": "d",
}
REF_KEY = "lr"
CODE_PATTERN = r"(?:[A-Za-z0-9_-]{16}|[a-z]{1,2}_[A-Za-z0-9_-]{11})"


def new_marker(platform):
    # 64 random bits, plus a readable channel code; at most 18 appended chars.
    return PLATFORM_CODES[platform] + "_" + secrets.token_urlsafe(8)


def attributed_url(destination, marker):
    parts = urlsplit(destination)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("请填写有效的公开网站链接")
    # Keep repeated query keys and fragments, replacing only our attribution.
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in (REF_KEY, "lead_ref")
    ]
    query.append((REF_KEY, marker))
    return urlunsplit(parts._replace(query=urlencode(query)))


def configuration(agent, business=None):
    saved = agent["onboarding_state"].get("promotion", {})
    return {
        "destination_url": saved.get("destination_url")
        or (business or {}).get("product_url")
        or "",
        "configured": bool(saved.get("destination_url")),
        "revision": saved.get("revision", 0),
    }


def promotion_body(body, destination, marker, product_url=None):
    """Replace product URLs in model copy with one configured, attributed CTA."""
    link = attributed_url(destination, marker)
    hosts = {urlsplit(url).hostname for url in (destination, product_url) if url}
    used = False

    def replace(match):
        nonlocal used
        if urlsplit(match[0]).hostname not in hosts:
            return match[0]
        value = "" if used else link
        used = True
        return value

    body = re.sub(r"https?://[^\s<>，。；）)\]]+", replace, body).strip()
    return (body if used else body + "\n\n" + link), link
