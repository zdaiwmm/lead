"""Canonical public community links from actual observed anchors."""

import base64
import re
from urllib.parse import parse_qs, urlsplit


def telegram_link(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    for _ in range(2):
        p = urlsplit(value)
        host = (p.hostname or "").lower()
        query = parse_qs(p.query)
        if host in ("www.google.com", "google.com") and p.path == "/url":
            value = (query.get("q") or query.get("url") or [""])[0]
            continue
        if host in ("www.bing.com", "bing.com") and p.path == "/ck/a":
            encoded = (query.get("u") or [""])[0]
            if not encoded.startswith("a1"):
                return None
            try:
                value = base64.urlsafe_b64decode(
                    encoded[2:] + "=" * ((-len(encoded[2:])) % 4)
                ).decode()
            except (ValueError, UnicodeError):
                return None
            continue
        if host in ("duckduckgo.com", "html.duckduckgo.com") and p.path == "/l/":
            value = (query.get("uddg") or [""])[0]
            continue
        break
    p = urlsplit(value)
    if (
        p.scheme not in ("https", "http")
        or p.username
        or p.password
        or p.hostname not in ("t.me", "telegram.me")
        or p.fragment
    ):
        return None
    if set(parse_qs(p.query)) - {"utm_source", "utm_medium", "utm_campaign", "ref"}:
        return None
    handle = p.path.strip("/")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", handle) or handle.lower() in {
        "share",
        "proxy",
        "login",
        "socks",
        "joinchat",
        "iv",
        "addstickers",
    }:
        return None
    return "https://t.me/" + handle
