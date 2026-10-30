"""Least privilege, immutable price admission and non-duplicating notification delivery."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
import psycopg
import pytest
from lead_app import admin_config as configs
from lead_app import admin_delivery as delivery
from lead_app import admin_notifications as notices
from lead_app import (
    admin_operations,
    admin_permissions,
    admin_pricing,
    admin_settings,
    budget,
    domain,
    identity_store,
)
from lead_app import api as api_module
from lead_app.config import database_kwargs
from lead_app.db import all_rows, one, tx, uid
from lead_app.scope import tenant_scope
from test_admin_phase3 import HEADERS, web

ISSUER = "https://identity.example"


@pytest.fixture
def workspace(monkeypatch):
    tenant = uid()
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'Phase four verification')",
            (tenant,),
        )
        c.execute(
            "INSERT INTO lead.memberships(tenant_id,oidc_issuer,oidc_subject,role) VALUES(%s,%s,'scoped-operator','operator'),(%s,%s,'scoped-viewer','viewer')",
            (tenant, ISSUER, tenant, ISSUER),
        )
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(admin_operations, "emit", lambda *a, **k: None)
    with tenant_scope(
        tenant,
        {"tenant_id": tenant, "role": "owner", "issuer": "local", "subject": "local"},
    ):
        yield tenant
        with tx() as c:
            c.execute("UPDATE lead.agents SET desired_state='archived'")


def publish(c, key, body, action="publish"):
    return configs.write(c, key, body, configs.read(c, key)["version"], action=action)


def actor(workspace, monkeypatch, role="operator"):
    who = {
        "tenant_id": workspace,
        "issuer": ISSUER,
        "subject": "scoped-" + role,
        "role": role,
    }
    monkeypatch.setattr(api_module, "auth_mode", lambda: "oidc")
    monkeypatch.setattr(identity_store, "lookup_session", lambda _: who.copy())
    return who


def test_member_draft_publish_rls_grant_revoke_and_global_guards(
    workspace, monkeypatch
):
    a, b = domain.create_agent(), domain.create_agent()
    body = {
        "members": [
            {
                "issuer": ISSUER,
                "subject": "scoped-operator",
                "capabilities": ["admin.models", "agents.chat", "accounts.manage"],
                "agent_ids": [a],
            }
        ]
    }
    with tx() as c:
        publish(c, "member-access/default", body, "draft")
    who = actor(workspace, monkeypatch)
    client = web()
    assert len(client.get("/api/agents").json()) == 2
    with tx() as c:
        publish(c, "member-access/default", body)
    assert [r["id"] for r in client.get("/api/agents").json()] == [a]
    assert client.get("/api/agents/" + b).status_code == 404
    assert client.get("/api/agents/" + b + "/browser/screenshot").status_code == 404
    assert (
        client.post(
            "/api/agents/" + b + "/browser/input", headers=HEADERS, json={}
        ).status_code
        == 404
    )
    assert client.get("/api/admin/model-usage", params={"aid": b}).status_code == 404
    assert (
        client.post(
            "/api/agents/" + b + "/messages",
            headers=HEADERS,
            json={"text": "not allowed", "client_message_id": uid()},
        ).status_code
        == 404
    )
    settings = admin_settings.defaults("models")
    result = client.post(
        "/api/admin/config",
        headers=HEADERS,
        json={"key": "models/" + a, "body": settings, "expected_version": 0},
    )
    assert result.status_code == 200, result.text
    assert (
        client.post(
            "/api/admin/config",
            headers=HEADERS,
            json={"key": "models/default", "body": settings, "expected_version": 0},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/agents/" + a + "/control", headers=HEADERS, json={"action": "pause"}
        ).status_code
        == 403
    )
    assert client.get("/api/admin/config?key=member-access/default").status_code == 403
    assert not any(
        row["config_key"] == "member-access/default"
        for row in client.get("/api/admin/history").json()
    )
    with tenant_scope(workspace, who):
        who.update(admin_permissions.resolve(who))
        with tx() as c:
            assert one(c, "SELECT id FROM lead.agents WHERE id=%s", (b,)) is None
            assert one(c, "SELECT count(*) AS n FROM lead.conversations")["n"] == 1
    body["members"][0]["capabilities"] = []
    with tx() as c:
        publish(c, "member-access/default", body)
    assert (
        client.post(
            "/api/admin/config",
            headers=HEADERS,
            json={"key": "models/" + a, "body": settings, "expected_version": 1},
        ).status_code
        == 403
    )


def test_viewer_scope_and_cross_workspace_policy_validation(workspace, monkeypatch):
    a, b = domain.create_agent(), domain.create_agent()
    with tx() as c:
        publish(
            c,
            "member-access/default",
            {
                "members": [
                    {
                        "issuer": ISSUER,
                        "subject": "scoped-viewer",
                        "capabilities": [],
                        "agent_ids": [a],
                    }
                ]
            },
        )
    actor(workspace, monkeypatch, "viewer")
    client = web()
    assert [row["id"] for row in client.get("/api/agents").json()] == [a]
    assert client.get("/api/agents/" + b).status_code == 404
    assert client.post("/api/admin/config", headers=HEADERS, json={}).status_code == 403
    with pytest.raises(ValueError, match="只读"):
        with tx() as c:
            publish(
                c,
                "member-access/default",
                {
                    "members": [
                        {
                            "issuer": ISSUER,
                            "subject": "scoped-viewer",
                            "capabilities": ["admin.models"],
                            "agent_ids": None,
                        }
                    ]
                },
            )
    with pytest.raises(ValueError, match="当前工作室"):
        with tx() as c:
            publish(
                c,
                "member-access/default",
                {
                    "members": [
                        {
                            "issuer": ISSUER,
                            "subject": "scoped-operator",
                            "capabilities": [],
                            "agent_ids": [uid()],
                        }
                    ]
                },
            )


def rates(input_rate="1", effective=None):
    now = datetime.now(timezone.utc) - timedelta(seconds=1)
    return {
        "currency": "USD",
        "effective_at": (effective or now).isoformat(),
        "models": [
            {
                "model": "openai:gpt-6-luna",
                "input_per_million": input_rate,
                "cached_input_per_million": "0.1",
                "output_per_million": "2",
                "max_request_input_tokens": 272000,
                "long_input_multiplier": "2",
                "long_output_multiplier": "1.5",
                "source_url": "https://developers.openai.com/api/docs/models/gpt-6-luna",
                "verified_at": now.isoformat(),
            }
        ],
    }


def test_price_admission_pins_version_drafts_future_effectivity_and_partial_unknown(
    workspace,
):
    aid = domain.create_agent()
    with tx() as c:
        publish(c, "pricing/default", rates())
    reservation = budget.reserve(aid, uid(), 1000, "openai:gpt-6-luna", "openai")
    with tx() as c:
        publish(c, "pricing/default", rates("10"), "draft")
        assert (
            admin_pricing.quote(c, "openai:gpt-6-luna")["version"]
            == "pricing/default@1"
        )
        publish(c, "pricing/default", rates("10"))
    details = {
        "model": "openai:gpt-6-luna",
        "provider": "openai",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 20,
        "cache_breakdown_verified": True,
        "requests": 1,
    }
    budget.settle(aid, reservation, actual=120, details=details)
    with tx(aid) as c:
        old = one(c, "SELECT details FROM lead.usage_events")["details"]
        assert old["rate_version"] == "pricing/default@1"
        assert Decimal(old["cost"]) == Decimal("0.000122")
        assert old["rate_snapshot"]["input_per_million"] == "1"
    with tx() as c:
        publish(
            c,
            "pricing/default",
            rates("20", datetime.now(timezone.utc) + timedelta(days=1)),
        )
        quote = admin_pricing.quote(c, "openai:gpt-6-luna")
        assert quote["version"] == "pricing/default@3"
        assert (
            admin_pricing.calculate(
                {**details, "unpriced_service_tier_requests": 1}, quote
            )["cost"]
            is None
        )
        assert (
            admin_pricing.calculate(
                {**details, "input_tokens": 300000, "requests": 2}, quote
            )["cost"]
            is None
        )
        long = admin_pricing.calculate(
            {**details, "input_tokens": 300000, "requests": 1}, quote
        )
        assert Decimal(long["cost"]) == Decimal("5.999664")
        assert (
            admin_pricing.calculate(
                {**details, "cache_breakdown_verified": False}, quote
            )["cost"]
            is None
        )
        assert (
            admin_pricing.calculate(
                details, admin_pricing.quote(c, "openai:unpriced-model")
            )["cost"]
            is None
        )


def destination(c, aid, monkeypatch, *, kind="webhook", enabled=True):
    from lead_app import fetch

    monkeypatch.setattr(
        fetch, "resolve_public", lambda value: (urlsplit(value), "203.0.113.8")
    )
    dest = uid()
    secret = delivery.DestinationSecret(
        url="https://notifications.example/hook?token=PRIVATE-DESTINATION",
        signing_secret="PRIVATE-SIGNATURE",
        kind=kind,
        smtp_host="smtp.example.com",
        smtp_username="private-user",
        smtp_password="PRIVATE-MAIL-PASSWORD",
        from_address="sender@example.com",
        to_addresses=["receiver@example.com"],
    )
    delivery.save_secret(c, dest, secret)
    config = {
        "enabled": enabled,
        "destinations": [
            delivery.Destination(
                id=dest,
                name="Private destination",
                kind=kind,
                enabled=True,
                agent_ids=[aid],
            ).model_dump(mode="json")
        ],
    }
    publish(c, "external-notifications/default", config)
    return dest, config


def observe(c, aid, key=None, settings=None):
    notices.condition(
        c,
        key or uid(),
        "login_expired",
        True,
        aid=aid,
        title="Real fixture alert",
        message="Fixture state needs attention",
        settings=settings or admin_settings.defaults("notifications"),
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_encrypted_destinations_durable_delivery_and_episode_dedup(
    workspace, monkeypatch
):
    aid = domain.create_agent()
    with tx() as c:
        dest, _ = destination(c, aid, monkeypatch)
        observe(c, aid, "same-episode")
        observe(c, aid, "same-episode")
        assert (
            one(c, "SELECT count(*) AS n FROM lead.notification_deliveries")["n"] == 1
        )
        ciphertext = bytes(
            one(c, "SELECT encrypted_payload FROM lead.notification_secrets")[
                "encrypted_payload"
            ]
        )
        assert b"PRIVATE" not in ciphertext
        history = json.dumps(configs.history(c), default=str)
        assert all(
            value not in history
            for value in [
                "PRIVATE-DESTINATION",
                "PRIVATE-SIGNATURE",
                "PRIVATE-MAIL-PASSWORD",
            ]
        )
        assert delivery.secret_status(c)[0]["version"] == 1
    sent = []

    async def send(secret, payload, event_id):
        sent.append((payload, event_id))
        return 200

    monkeypatch.setattr(delivery, "webhook", send)
    await delivery.dispatch()
    await delivery.dispatch()
    with tx() as c:
        row = one(c, "SELECT * FROM lead.notification_deliveries")
        assert row["state"] == "delivered" and row["attempts"] == 1 and len(sent) == 1
        assert sent[0][1] == str(row["notification_id"])
    assert (
        "PRIVATE" not in web().get("/api/admin/external-notifications/deliveries").text
    )
    assert "PRIVATE" not in web().get("/api/admin/external-notifications/secrets").text
    with pytest.raises(ValueError, match="已被更新"):
        with tx() as c:
            delivery.save_secret(
                c, dest, delivery.DestinationSecret(url="https://new.example/hook")
            )


@pytest.mark.asyncio
async def test_delivery_retries_are_bounded_unknown_requires_review_and_disable_cancels(
    workspace, monkeypatch
):
    aid = domain.create_agent()
    with tx() as c:
        _, config = destination(c, aid, monkeypatch)
        observe(c, aid)

    async def unavailable(*args):
        return 503

    monkeypatch.setattr(delivery, "webhook", unavailable)
    for _ in range(3):
        with tx() as c:
            c.execute(
                "UPDATE lead.notification_deliveries SET available_at=now()-interval '1 second'"
            )
        await delivery.dispatch()
    with tx() as c:
        failed = one(c, "SELECT * FROM lead.notification_deliveries")
        assert failed["state"] == "failed" and failed["attempts"] == 3
        observe(c, aid)

    async def uncertain(*args):
        raise httpx.ReadTimeout("sensitive provider message never persisted")

    monkeypatch.setattr(delivery, "webhook", uncertain)
    await delivery.dispatch()
    with tx() as c:
        unknown = one(
            c, "SELECT * FROM lead.notification_deliveries WHERE state='unknown'"
        )
        assert unknown["last_error_code"] == "delivery_result_unknown"
    client = web()
    assert (
        client.post(
            "/api/admin/external-notifications/deliveries/"
            + str(unknown["id"])
            + "/retry",
            headers=HEADERS,
            json={"expected_attempts": 1},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/admin/external-notifications/deliveries/"
            + str(unknown["id"])
            + "/retry",
            headers=HEADERS,
            json={"expected_attempts": 1, "acknowledge_duplicate_risk": True},
        ).status_code
        == 200
    )
    with tx() as c:
        config["enabled"] = False
        publish(c, "external-notifications/default", config)
    await delivery.dispatch()
    with tx() as c:
        assert (
            one(
                c,
                "SELECT state FROM lead.notification_deliveries WHERE id=%s",
                (unknown["id"],),
            )["state"]
            == "skipped"
        )


@pytest.mark.asyncio
async def test_webhook_transport_pins_dns_signs_and_does_not_follow_redirects(
    monkeypatch,
):
    from lead_app import fetch

    monkeypatch.setattr(
        fetch, "resolve_public", lambda value: (urlsplit(value), "93.184.216.34")
    )
    original = httpx.AsyncClient
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(
            302, headers={"location": "https://private.internal/secret"}
        )

    monkeypatch.setattr(
        delivery.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)),
    )
    status = await delivery.webhook(
        {
            "url": "https://notifications.example/hook?secret=fixture",
            "signing_secret": "fixture-signing",
        },
        {"title": "verify"},
        uid(),
    )
    request = captured[0]
    assert status == 302 and len(captured) == 1
    assert (
        request.url.host == "93.184.216.34"
        and request.headers["host"] == "notifications.example"
    )
    assert request.extensions["sni_hostname"] == "notifications.example"
    assert request.headers["x-lead-signature"].startswith("sha256=")


def test_real_public_only_destination_validation_and_owner_gate(workspace, monkeypatch):
    with pytest.raises(ValueError, match="公开"):
        with tx() as c:
            delivery.save_secret(
                c, uid(), delivery.DestinationSecret(url="https://127.0.0.1/private")
            )
    actor(workspace, monkeypatch)
    client = web()
    assert client.get("/api/admin/external-notifications/secrets").status_code == 403
    assert (
        client.post(
            "/api/admin/external-notifications/secrets/" + uid(),
            headers=HEADERS,
            json={"url": "https://notifications.example"},
        ).status_code
        == 403
    )


def test_email_transport_uses_tls_pinned_host_and_reports_partial_recipients(
    workspace, monkeypatch
):
    import socket

    from lead_app import fetch

    monkeypatch.setattr(
        fetch, "resolve_public", lambda value: (urlsplit(value), "93.184.216.34")
    )
    events = []

    class Socket:
        def close(self):
            events.append("closed")

    class Context:
        def wrap_socket(self, raw, server_hostname):
            events.append(("tls", server_hostname))
            return raw

    monkeypatch.setattr(delivery.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, timeout: (events.append(("tcp", address)), Socket())[1],
    )

    def connect(self, host="", port=0, source_address=None):
        self.sock = self._get_socket(host, port, 15)
        return 220, b"fixture"

    monkeypatch.setattr(delivery.smtplib.SMTP_SSL, "connect", connect)
    monkeypatch.setattr(
        delivery.smtplib.SMTP_SSL,
        "login",
        lambda self, user, password: events.append(("login", user)),
    )

    def send(self, message):
        events.append(("message", message["Message-ID"], message["To"]))
        return {"bad@example.com": (550, b"fixture")}

    monkeypatch.setattr(delivery.smtplib.SMTP_SSL, "send_message", send)
    secret = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_username": "owner",
        "smtp_password": "fixture",
        "from_address": "sender@example.com",
        "to_addresses": ["receiver@example.com"],
    }
    status = delivery.email(secret, {"title": "Notice", "message": "Body"}, "event-id")
    assert status == 207
    assert ("tcp", ("93.184.216.34", 465)) in events and (
        "tls",
        "smtp.example.com",
    ) in events
    assert (
        "message",
        "<event-id@lead.notifications>",
        "receiver@example.com",
    ) in events


@pytest.mark.asyncio
async def test_openai_cache_writes_use_reported_lifetimes_without_double_billing(
    workspace, monkeypatch
):
    from lead_app import metering, model
    from openai import AsyncOpenAI
    from pydantic import BaseModel

    class Answer(BaseModel):
        reply: str

    aid = domain.create_agent()
    monkeypatch.setenv("OPENAI_API_KEY", "cache-usage-fixture")
    with tx() as c:
        publish(c, "model-catalog/default", {"openai_models": ["gpt-6-luna"]})
        settings = admin_settings.defaults("models")
        settings["default_model"] = "openai:gpt-6-luna"
        publish(c, "models/" + aid, settings)
        prices = rates()
        prices["models"][0].update(
            cache_write_30m_per_million="1.25", cache_write_12h_per_million="2.5"
        )
        publish(c, "pricing/default", prices)
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        details = {"cached_tokens": 4000, "cache_write_tokens": 7000}
        if len(calls) == 1:
            details["cache_write_tokens_details"] = {
                "ttl_30m_tokens": 3000,
                "ttl_12h_tokens": 4000,
            }
        if len(calls) == 3:
            details.pop("cache_write_tokens")
        return httpx.Response(
            200,
            json={
                "id": "resp_fixture",
                "object": "response",
                "created_at": 1793318400,
                "status": "completed",
                "model": "gpt-6-luna",
                "output": [
                    {
                        "id": "msg_fixture",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"reply":"ok"}',
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 12000,
                    "input_tokens_details": details,
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 12020,
                },
            },
        )

    monkeypatch.setattr(
        model,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ),
    )
    for _ in range(3):
        assert (
            await model.run_model(Answer, "Return ok", agent_id=aid, purpose="chat")
        ).reply == "ok"
    with tx(aid) as c:
        rows = all_rows(
            c, "SELECT details FROM lead.usage_events ORDER BY occurred_at,id"
        )
        first, second, missing = [row["details"] for row in rows]
        assert Decimal(first["cost"]) == Decimal("0.01519")
        assert (
            first["cache_write_30m_tokens"] == 3000
            and first["cache_write_12h_tokens"] == 4000
        )
        assert Decimal(second["cost"]) == Decimal("0.01019")
        assert (
            second["cache_write_30m_tokens"] == 7000
            and second["cache_write_12h_tokens"] == 0
        )
        assert (
            missing["cost"] is None and missing["unmeasured_cache_write_requests"] == 1
        )
        assert missing["rate_version"] == "pricing/default@1"
    summary = metering.summary(aid)["by_model"][0]
    assert summary["tokens"] == 36060 and summary["cache_write_tokens"] == 14000
    assert (
        summary["cache_write_30m_tokens"] == 10000
        and summary["cache_write_12h_tokens"] == 4000
    )
    assert summary["unpriced_calls"] == 1
