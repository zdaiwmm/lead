"""Storage minimization for automatically collected page URLs."""

import re
from urllib.parse import urlsplit, urlunsplit


def page_url(value):
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    if parts.port:
        host += ":" + str(parts.port)
    path = re.sub(
        r"(?i)(/(?:reset(?:-password)?|verify|magic-link|invite|token)/)[^/]+",
        r"\1_redacted",
        parts.path,
    )
    # Attribution is held in explicit ref/click fields. Query strings and
    # fragments may contain credentials or customer information; none are kept.
    return urlunsplit((parts.scheme, host, path, "", ""))


def redact_payload(payload):
    result = dict(payload)
    for key in (
        "key",
        "write_key",
        "test_code",
        "click_token",
        "lead_attribution",
        "user_id",
    ):
        result.pop(key, None)
    if result.get("url"):
        result["url"] = page_url(result["url"])
    return result


def redact_context(value):
    result = dict(value)
    if result.get("page_path"):
        result["page_path"] = urlsplit(
            page_url("https://redact.invalid/" + result["page_path"].lstrip("/"))
        ).path
    return result
