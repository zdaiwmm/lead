"""Platform-managed sticky exits. No credentials are returned to the user or model."""

import ipaddress
import json
import os
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.rows import dict_row

from .config import STATE, database_kwargs
from .db import tx
from .identity_store import auth_tx


def key():
    path = STATE / "keys/network.key"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        temporary = path.with_name(".network-" + uuid4().hex)
        try:
            with temporary.open("xb") as f:
                os.chmod(temporary, 0o600)
                f.write(os.urandom(32))
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("环境凭据存储不可用")
    value = path.read_bytes()
    if len(value) != 32:
        raise ValueError("环境凭据密钥不可用")
    return value


def parse(value):
    from urllib.parse import unquote, urlsplit

    try:
        p = urlsplit(value)
        port = p.port
    except ValueError:
        raise ValueError("出口地址不可用") from None
    if (
        p.scheme not in ("http", "https", "socks5")
        or not p.hostname
        or not port
        or p.query
        or p.fragment
        or p.path not in ("", "/")
    ):
        raise ValueError("出口地址不可用")
    host = "[" + p.hostname + "]" if ":" in p.hostname else p.hostname
    result = {"server": f"{p.scheme}://{host}:{p.port}"}
    if p.username:
        result.update(username=unquote(p.username), password=unquote(p.password or ""))
    return result


def add(label, url, expected_ip, region=None, timezone=None, locale=None, expires=None):
    address = ipaddress.ip_address(expected_ip)
    if not address.is_global:
        raise ValueError("出口需要可核验的固定公网地址")
    if timezone:
        ZoneInfo(timezone)
    if region and (len(region) != 2 or not region.isalpha()):
        raise ValueError("地区需要两位国家代码")
    identifier = str(uuid4())
    nonce = os.urandom(12)
    cipher = nonce + AESGCM(key()).encrypt(
        nonce, json.dumps(parse(url)).encode(), identifier.encode()
    )
    # Store id with authenticated ciphertext so it cannot be moved to another row.
    envelope = identifier.encode() + b"\n" + cipher
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.proxy_pool(id,label,cipher,expected_ip,region,timezone,locale,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                identifier,
                label,
                envelope,
                str(address),
                region.upper() if region else None,
                timezone,
                locale,
                expires,
            ),
        )
    return {"id": identifier, "label": label}


def claim(aid):
    with tx(aid) as c:
        result = c.execute("SELECT lead.claim_proxy(%s) AS state", (aid,)).fetchone()[
            "state"
        ]
    if result == "absent":
        raise LookupError("Agent 不存在")
    if result == "queued":
        raise NetworkWaiting("运营环境正在排队，平台会自动重试")
    return result


class NetworkWaiting(ValueError):
    code = "browser_busy"


def policy(aid):
    with auth_tx() as c:
        row = c.execute("SELECT * FROM lead.resolve_proxy(%s)", (aid,)).fetchone()
    return row


def resolve(aid):
    claim(aid)
    row = policy(aid)
    if not row:
        return None
    if not row["usable"]:
        raise NetworkWaiting("此环境的出口暂不可用，已停止新的操作")
    identifier, payload = bytes(row["cipher"]).split(b"\n", 1)
    try:
        return json.loads(AESGCM(key()).decrypt(payload[:12], payload[12:], identifier))
    except Exception:
        raise NetworkWaiting("环境凭据需要平台维护，已停止新的操作") from None


def inventory():
    with psycopg.connect(**database_kwargs(True), row_factory=dict_row) as c:
        return c.execute(
            "SELECT p.id,p.label,p.expected_ip,p.region,p.timezone,p.locale,p.state,p.expires_at,count(a.id) FILTER(WHERE a.released_at IS NULL) AS assigned FROM lead.proxy_pool p LEFT JOIN lead.proxy_assignments a ON a.pool_id=p.id GROUP BY p.id ORDER BY p.created_at"
        ).fetchall()
