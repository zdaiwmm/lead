"""Encrypted destinations and a bounded, inspectable notification outbox."""

import asyncio
import hashlib
import hmac
import json
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Literal
from uuid import UUID

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import CONFIG
from .db import all_rows, audit, j, one, tx, uid
from .scope import current_tenant

CATEGORIES = [
    "budget_warning",
    "budget_exhausted",
    "login_expired",
    "repeated_failures",
    "uncertain_action",
    "knowledge_expired",
    "measurement_stale",
]


class Destination(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["webhook", "email"] = "webhook"
    enabled: bool = False
    categories: list[str] = Field(
        default_factory=lambda: CATEGORIES.copy(), max_length=7
    )
    severities: list[Literal["warning", "error"]] = Field(
        default_factory=lambda: ["warning", "error"]
    )
    agent_ids: list[UUID] | None = Field(default=None, max_length=1000)
    max_attempts: int = Field(default=3, strict=True, ge=1, le=5)
    retry_seconds: int = Field(default=60, strict=True, ge=30, le=3600)

    @field_validator("categories")
    @classmethod
    def known(cls, value):
        if not set(value) <= set(CATEGORIES) or len(set(value)) != len(value):
            raise ValueError("通知类别无效或重复")
        return value


class ExternalNotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    destinations: list[Destination] = Field(default_factory=list, max_length=20)

    @field_validator("destinations")
    @classmethod
    def unique(cls, value):
        if len(set(d.id for d in value)) != len(value):
            raise ValueError("通知渠道标识不能重复")
        return value


class DestinationSecret(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(default=0, strict=True, ge=0)
    kind: Literal["webhook", "email"] = "webhook"
    url: str = Field(default="", max_length=3000)
    signing_secret: str = Field(default="", max_length=500)
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: Literal[465, 587] = 465
    smtp_username: str = Field(default="", max_length=320)
    smtp_password: str = Field(default="", max_length=1000)
    from_address: str = Field(default="", max_length=320)
    to_addresses: list[str] = Field(default_factory=list, max_length=20)


def cipher():
    return AESGCM(
        hashlib.sha256(
            ("lead-notifications:" + CONFIG["session_secret"]).encode()
        ).digest()
    )


def aad(destination):
    return (current_tenant() + ":" + str(destination)).encode()


def secret_status(c):
    return all_rows(
        c,
        "SELECT destination_id AS id,version,updated_at FROM lead.notification_secrets ORDER BY updated_at DESC",
    )


def save_secret(c, destination, body):
    from .fetch import resolve_public

    data = body.model_dump(exclude={"expected_version"})
    if body.kind == "webhook":
        p, _ = resolve_public(body.url)
        if p.scheme != "https" or p.fragment:
            raise ValueError("Webhook 必须使用公开 HTTPS 地址且不能包含片段")
    else:
        import re

        if not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", body.smtp_host
        ):
            raise ValueError("SMTP 主机请只填写公开域名，不含路径、端口或登录信息")
        resolve_public("https://" + body.smtp_host)
        if (
            not body.smtp_host
            or not body.smtp_username
            or not body.smtp_password
            or not body.to_addresses
        ):
            raise ValueError("请补齐 SMTP 服务、登录凭据与接收邮箱")
        if any(
            not re.fullmatch(
                r"[^\s<>@,;\r\n]+@[^\s<>@,;\r\n]+\.[^\s<>@,;\r\n]+", address
            )
            for address in [body.from_address, *body.to_addresses]
        ):
            raise ValueError("请填写有效且不含换行的邮箱地址")
    c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(lead.ctx_tenant()::text || %s,0))",
        (str(destination),),
    )
    old = one(
        c,
        "SELECT version FROM lead.notification_secrets WHERE destination_id=%s FOR UPDATE",
        (destination,),
    )
    if (old["version"] if old else 0) != body.expected_version:
        raise ValueError("凭据已被更新，请刷新后重新核对")
    version = body.expected_version + 1
    nonce = secrets.token_bytes(12)
    encrypted = nonce + cipher().encrypt(
        nonce, json.dumps(data).encode(), aad(destination)
    )
    c.execute(
        "INSERT INTO lead.notification_secrets(tenant_id,destination_id,encrypted_payload,version) VALUES(lead.ctx_tenant(),%s,%s,%s) ON CONFLICT(tenant_id,destination_id) DO UPDATE SET encrypted_payload=excluded.encrypted_payload,version=excluded.version,updated_at=now()",
        (destination, encrypted, version),
    )
    audit(
        c,
        None,
        "admin.notification_secret.changed",
        {"destination_id": str(destination), "version": version, "kind": body.kind},
    )
    return {"id": str(destination), "version": version, "configured": True}


def load_secret(c, destination):
    row = one(
        c,
        "SELECT encrypted_payload FROM lead.notification_secrets WHERE destination_id=%s",
        (destination,),
    )
    if not row:
        return None
    data = bytes(row["encrypted_payload"])
    return json.loads(cipher().decrypt(data[:12], data[12:], aad(destination)))


def validate(c, data):
    for destination in data["destinations"]:
        if destination["agent_ids"] is not None:
            ids = destination["agent_ids"]
            if len(set(ids)) != len(ids) or len(
                all_rows(
                    c,
                    "SELECT id FROM lead.agents WHERE id=ANY(%s::uuid[]) AND desired_state<>'archived'",
                    (ids,),
                )
            ) != len(ids):
                raise ValueError("通知 Agent 范围必须属于当前工作室")
        if data["enabled"] and destination["enabled"]:
            secret = load_secret(c, destination["id"])
            if not secret or secret["kind"] != destination["kind"]:
                raise ValueError("启用外部渠道前，请由所有者保存匹配的渠道凭据")


def enqueue(c, notification):
    row = one(
        c,
        "SELECT body,published_version FROM lead.admin_configs WHERE config_key='external-notifications/default'",
    )
    if not row or not row["body"] or not row["body"]["enabled"]:
        return
    for destination in row["body"]["destinations"]:
        if (
            not destination["enabled"]
            or notification["category"] not in destination["categories"]
            or notification["severity"] not in destination["severities"]
        ):
            continue
        if (
            destination["agent_ids"] is not None
            and str(notification["agent_id"]) not in destination["agent_ids"]
        ):
            continue
        c.execute(
            "INSERT INTO lead.notification_deliveries(tenant_id,agent_id,notification_id,destination_id,configuration_version,destination_snapshot,available_at,attempt_limit) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                notification["agent_id"],
                notification["id"],
                destination["id"],
                "external-notifications/default@" + str(row["published_version"]),
                j(destination),
                notification["available_at"],
                destination["max_attempts"],
            ),
        )


async def webhook(secret, payload, event_id):
    from .fetch import resolve_public

    p, ip = await asyncio.to_thread(resolve_public, secret["url"])
    if p.scheme != "https":
        raise ValueError("destination_not_https")
    authority = "[" + ip + "]" if ":" in ip else ip
    url = p._replace(netloc=authority + (":" + str(p.port) if p.port else "")).geturl()
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        "Host": p.hostname,
        "Content-Type": "application/json",
        "Idempotency-Key": event_id,
        "X-Lead-Event-ID": event_id,
    }
    if secret["signing_secret"]:
        timestamp = str(int(time.time()))
        headers["X-Lead-Timestamp"] = timestamp
        headers["X-Lead-Signature"] = (
            "sha256="
            + hmac.new(
                secret["signing_secret"].encode(),
                timestamp.encode() + b"." + data,
                hashlib.sha256,
            ).hexdigest()
        )
    async with httpx.AsyncClient(
        timeout=15, trust_env=False, follow_redirects=False
    ) as client:
        request = client.build_request("POST", url, content=data, headers=headers)
        request.extensions["sni_hostname"] = p.hostname
        response = await client.send(request, stream=True)
        try:
            return response.status_code
        finally:
            await response.aclose()


def email(secret, payload, event_id):
    from .fetch import resolve_public

    _, ip = resolve_public("https://" + secret["smtp_host"])
    context = ssl.create_default_context()

    # Resolve once and pin the TCP connection while retaining hostname TLS checks.
    class PinnedSMTP(smtplib.SMTP):
        def _get_socket(self, host, port, timeout):
            import socket

            return socket.create_connection((ip, port), timeout)

    class PinnedSSL(smtplib.SMTP_SSL):
        def _get_socket(self, host, port, timeout):
            import socket

            raw = socket.create_connection((ip, port), timeout)
            return self.context.wrap_socket(raw, server_hostname=host)

    message = EmailMessage()
    message["Subject"] = "lead · " + payload["title"].replace("\r", " ").replace(
        "\n", " "
    )
    message["From"] = secret["from_address"]
    message["To"] = ", ".join(secret["to_addresses"])
    message["Message-ID"] = "<" + event_id + "@lead.notifications>"
    message.set_content(payload["message"] + "\n\n通知编号：" + event_id)
    client = (
        PinnedSSL(secret["smtp_host"], 465, timeout=15, context=context)
        if secret["smtp_port"] == 465
        else PinnedSMTP(secret["smtp_host"], 587, timeout=15)
    )
    try:
        if secret["smtp_port"] == 587:
            client.starttls(context=context)
        client.login(secret["smtp_username"], secret["smtp_password"])
        rejected = client.send_message(message)
        return 207 if rejected else 200
    finally:
        client.close()


def claim(c):
    # Never automatically repeat an attempt that might have reached a recipient.
    c.execute(
        "UPDATE lead.notification_deliveries SET state='unknown',last_error_code='delivery_interrupted',lease_until=NULL WHERE state='sending' AND lease_until<now()"
    )
    row = one(
        c,
        "SELECT d.*,n.title,n.message,n.category,n.severity,n.resolved_at FROM lead.notification_deliveries d JOIN lead.admin_notifications n ON n.id=d.notification_id WHERE d.state IN ('pending','retry') AND d.available_at<=now() ORDER BY d.available_at,d.id FOR UPDATE OF d SKIP LOCKED LIMIT 1",
    )
    if not row:
        return None
    current = one(
        c,
        "SELECT body FROM lead.admin_configs WHERE config_key='external-notifications/default'",
    )
    destination = next(
        (
            d
            for d in (
                current["body"]["destinations"]
                if current and current["body"] and current["body"]["enabled"]
                else []
            )
            if d["id"] == str(row["destination_id"]) and d["enabled"]
        ),
        None,
    )
    if (
        not destination
        or row["resolved_at"]
        or destination["kind"] != row["destination_snapshot"]["kind"]
    ):
        c.execute(
            "UPDATE lead.notification_deliveries SET state='skipped',last_error_code='disabled_or_resolved',completed_at=now() WHERE id=%s",
            (row["id"],),
        )
        return {"skip": True}
    row["secret"] = load_secret(c, row["destination_id"])
    if not row["secret"] or row["secret"]["kind"] != destination["kind"]:
        c.execute(
            "UPDATE lead.notification_deliveries SET state='failed',last_error_code='destination_not_configured',completed_at=now() WHERE id=%s",
            (row["id"],),
        )
        return {"skip": True}
    row["attempt_token"] = uid()
    c.execute(
        "UPDATE lead.notification_deliveries SET state='sending',attempts=attempts+1,attempt_token=%s,lease_until=now()+interval '2 minutes' WHERE id=%s",
        (row["attempt_token"], row["id"]),
    )
    return row


def finish(c, row, state, status=None, error=None):
    attempts = row["attempts"] + 1
    policy = row["destination_snapshot"]
    if state == "retry" and attempts >= row["attempt_limit"]:
        state = "failed"
    c.execute(
        "UPDATE lead.notification_deliveries SET state=%s,last_status_code=%s,last_error_code=%s,lease_until=NULL,available_at=now()+make_interval(secs=>%s),completed_at=CASE WHEN %s IN ('delivered','failed','skipped') THEN now() ELSE NULL END WHERE id=%s AND state='sending' AND attempt_token=%s",
        (
            state,
            status,
            error,
            min(3600, policy["retry_seconds"] * 2 ** (attempts - 1)),
            state,
            row["id"],
            row["attempt_token"],
        ),
    )


async def dispatch(limit=10):
    for _ in range(limit):
        with tx() as c:
            row = claim(c)
        if not row:
            return
        if row.get("skip"):
            continue
        payload = {k: row[k] for k in ("title", "message", "category", "severity")}
        payload.update(
            event_id=str(row["notification_id"]),
            agent_id=str(row["agent_id"]) if row["agent_id"] else None,
        )
        status = None
        try:
            status = (
                await webhook(row["secret"], payload, str(row["notification_id"]))
                if row["secret"]["kind"] == "webhook"
                else await asyncio.to_thread(
                    email, row["secret"], payload, str(row["notification_id"])
                )
            )
            state = (
                "delivered"
                if 200 <= status < 300 and status != 207
                else "retry"
                if status == 429 or status >= 500
                else "failed"
            )
            error = (
                None
                if state == "delivered"
                else "partial_recipient_failure"
                if status == 207
                else "http_rejected"
            )
        except (ValueError, smtplib.SMTPAuthenticationError):
            state, error = "failed", "destination_validation_failed"
        except Exception:
            state, error = "unknown", "delivery_result_unknown"
        with tx() as c:
            finish(c, row, state, status, error)
