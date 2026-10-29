import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2 as httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from lead_app import auth, domain
from lead_app.api import app
from lead_app.config import database_kwargs
from lead_app.db import all_rows, tx, uid
from lead_app.identity_store import agent_tenant, lookup_session
from lead_app.scope import current_tenant, scoped_activity, tenant_scope
from lead_app.security import token


@pytest.mark.asyncio
async def test_existing_event_stream_stops_after_session_revocation(provider):
    from lead_app.events import stream

    client = provider.login("stream-owner")
    secret = client.cookies.get("lead_session")
    identity = lookup_session(secret)

    class EventRequest:
        headers = {}
        cookies = {"lead_session": secret}

        async def is_disconnected(self):
            return False

    with tenant_scope(identity["tenant_id"], identity):
        iterator = stream(EventRequest())
        assert "retry" in await anext(iterator)
        assert "id:" in await anext(iterator)
        auth.revoke(secret)
        assert "refresh" in await anext(iterator)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)


class Provider:
    def __init__(self):
        self.issuer = "https://identity.example/" + uuid4().hex
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = {
            **jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True),
            "kid": "fixture",
            "use": "sig",
            "alg": "RS256",
        }
        self.flows = {}
        self.calls = []

    def route(self, request):
        self.calls.append(request.url.path)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": self.issuer + "/authorize",
                    "token_endpoint": self.issuer + "/token",
                    "jwks_uri": self.issuer + "/keys",
                    "code_challenge_methods_supported": ["S256"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path.endswith("/keys"):
            return httpx.Response(200, json={"keys": [self.jwk]})
        if request.url.path.endswith("/token"):
            body = parse_qs(request.content.decode())
            flow = self.flows[body["code"][0]]
            verifier = body["code_verifier"][0]
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            assert challenge == flow["challenge"]
            assert body["redirect_uri"] == ["https://testserver/api/auth/callback"]
            assert request.headers["authorization"].startswith("Basic ")
            claims = {
                "iss": self.issuer,
                "sub": flow["subject"],
                "aud": "lead-test",
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "nonce": flow["nonce"],
                **flow["changes"],
            }
            key = self.key
            algorithm = "RS256"
            if flow["changes"].get("_wrong_signature"):
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if flow["changes"].get("_algorithm"):
                algorithm = flow["changes"]["_algorithm"]
                key = "synthetic-hmac-test-key-with-adequate-length"
            raw = jwt.encode(
                claims, key, algorithm=algorithm, headers={"kid": "fixture"}
            )
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-test-access-token",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "id_token": raw,
                },
            )
        return httpx.Response(404)

    def begin(self, client, subject, changes=None):
        response = client.get("/api/auth/login", follow_redirects=False)
        assert response.status_code == 302
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert (
            query["code_challenge_method"] == ["S256"] and "code_verifier" not in query
        )
        code = secrets.token_urlsafe(20)
        self.flows[code] = {
            "subject": subject,
            "nonce": query["nonce"][0],
            "challenge": query["code_challenge"][0],
            "changes": changes or {},
        }
        assert (
            "HttpOnly" in response.headers["set-cookie"]
            and "Secure" in response.headers["set-cookie"]
        )
        return "/api/auth/callback?code=" + code + "&state=" + query["state"][0]

    def client(self):
        return TestClient(
            app,
            base_url="https://testserver",
            headers={"Origin": "https://testserver", "X-Lead-Request": "1"},
        )

    def login(self, subject, changes=None):
        client = self.client()
        response = client.get(
            self.begin(client, subject, changes), follow_redirects=False
        )
        assert response.status_code == 302
        assert response.headers["location"] == "https://testserver/"
        return client


@pytest.fixture
def provider(monkeypatch):
    instance = Provider()
    monkeypatch.setenv("LEAD_AUTH_MODE", "oidc")
    monkeypatch.setenv("LEAD_OIDC_ISSUER", instance.issuer)
    monkeypatch.setenv("LEAD_OIDC_CLIENT_ID", "lead-test")
    monkeypatch.setenv("LEAD_OIDC_CLIENT_SECRET", "synthetic-test-client-secret")
    monkeypatch.setenv("LEAD_PUBLIC_URL", "https://testserver")
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    transport = httpx.MockTransport(instance.route)
    factory = auth.oauth_client
    public = auth.public_client
    monkeypatch.setattr(
        auth, "oauth_client", lambda cfg: factory(cfg, transport=transport)
    )
    monkeypatch.setattr(auth, "public_client", lambda: public(transport=transport))
    yield instance
    with psycopg.connect(**database_kwargs(True)) as c:
        tenants = [
            str(row[0])
            for row in c.execute(
                "SELECT DISTINCT tenant_id FROM lead.memberships WHERE oidc_issuer=%s",
                (instance.issuer,),
            ).fetchall()
        ]
        for tenant in tenants:
            with tenant_scope(tenant):
                with tx() as runtime:
                    agents = all_rows(
                        runtime,
                        "SELECT id FROM lead.agents WHERE desired_state<>'archived'",
                    )
                for agent in agents:
                    domain.set_control(str(agent["id"]), "archive")
            c.execute(
                "UPDATE lead.auth_sessions SET revoked_at=now() WHERE tenant_id=%s",
                (tenant,),
            )
            c.execute("UPDATE lead.tenants SET deleted_at=now() WHERE id=%s", (tenant,))
    agent_tenant.cache_clear()


def create(client, key=None):
    r = client.post("/api/agents", json={"client_id": key or str(uuid4())})
    assert r.status_code == 201
    return r.json()["id"]


def test_oidc_real_signatures_pkce_sessions_and_cross_tenant_api_isolation(provider):
    alice = provider.login("alice")
    bob = provider.login("bob")
    same_key = str(uuid4())
    a = create(alice, same_key)
    b = create(bob, same_key)
    assert a != b
    assert create(alice, same_key) == a
    assert {x["id"] for x in alice.get("/api/agents").json()} == {a}
    assert {x["id"] for x in bob.get("/api/agents").json()} == {b}
    assert alice.get("/api/agents/" + b).status_code == 404
    assert (
        alice.post(
            "/api/agents/" + b + "/messages",
            json={"text": "cross tenant", "client_message_id": uid()},
        ).status_code
        == 404
    )
    assert alice.get("/api/agents/" + b + "/browser/screenshot").status_code == 404
    assert (
        alice.post(
            "/api/workspace/switch",
            json={"tenant_id": bob.get("/api/auth/me").json()["tenant_id"]},
        ).status_code
        == 404
    )
    secret = alice.cookies["lead_session"]
    assert secret.startswith("lead_s_") and "synthetic-test-access-token" not in secret
    with psycopg.connect(**database_kwargs(True)) as c:
        stored = c.execute(
            "SELECT secret_sha256 FROM lead.auth_sessions WHERE subject=%s AND issuer=%s",
            ("alice", provider.issuer),
        ).fetchone()[0]
        assert (
            stored == hashlib.sha256(secret.encode()).hexdigest() and stored != secret
        )
    assert alice.post("/api/auth/logout").status_code == 200
    assert alice.get("/api/agents").status_code == 401
    assert lookup_session(secret) is None


def test_membership_roles_immediate_revocation_and_last_owner_guard(provider):
    owner = provider.login("owner")
    aid = create(owner)
    assert (
        owner.post(
            "/api/workspace/members", json={"subject": "viewer", "role": "viewer"}
        ).status_code
        == 200
    )
    viewer = provider.login("viewer")
    assert viewer.get("/api/auth/me").json()["role"] == "viewer"
    assert viewer.get("/api/agents/" + aid).status_code == 200
    assert viewer.post("/api/agents", json={}).status_code == 403
    assert viewer.get("/api/agents/" + aid + "/browser/screenshot").status_code == 403
    assert (
        owner.post(
            "/api/workspace/members", json={"subject": "owner", "role": "viewer"}
        ).status_code
        == 409
    )
    assert (
        owner.post(
            "/api/workspace/members", json={"subject": "viewer", "role": "operator"}
        ).status_code
        == 200
    )
    assert viewer.get("/api/auth/me").json()["role"] == "operator"
    create(viewer)
    assert viewer.get("/api/workspace/members").status_code == 403
    assert (
        owner.post(
            "/api/workspace/members",
            json={"subject": "viewer", "role": "operator", "status": "revoked"},
        ).status_code
        == 200
    )
    assert viewer.get("/api/agents").status_code == 401
    old = provider.client()
    old.cookies.set("lead_session", token("owner"))
    assert old.post("/api/session").json()["ok"] is False
    assert old.get("/api/agents").status_code == 401


@pytest.mark.parametrize(
    "changes",
    [
        {"nonce": "wrong"},
        {"aud": "another-client"},
        {"iss": "https://evil.example"},
        {"exp": 1},
        {"_wrong_signature": True},
        {"_algorithm": "HS256"},
        {"aud": ["lead-test", "other"], "azp": "other"},
    ],
)
def test_bad_identity_tokens_never_create_owner_session(provider, changes):
    client = provider.client()
    response = client.get(
        provider.begin(client, "invalid-user", changes), follow_redirects=False
    )
    assert response.status_code == 401
    assert not client.cookies.get("lead_session")


def test_login_state_is_browser_bound_one_time_and_expires(provider):
    client = provider.client()
    other = provider.client()
    url = provider.begin(client, "alice")
    assert other.get(url, follow_redirects=False).status_code == 401
    assert client.get(url, follow_redirects=False).status_code == 302
    assert client.get(url, follow_redirects=False).status_code == 401
    again = provider.begin(client, "alice")
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "UPDATE lead.oauth_transactions SET expires_at=now()-interval '1 minute' WHERE state_sha256=%s",
            (
                hashlib.sha256(
                    parse_qs(urlsplit(again).query)["state"][0].encode()
                ).hexdigest(),
            ),
        )
    assert client.get(again, follow_redirects=False).status_code == 401


@pytest.mark.asyncio
async def test_background_activity_rebinds_scope_and_restores_caller(provider):
    alice = provider.login("alice")
    bob = provider.login("bob")
    a = create(alice)
    b = create(bob)
    ta = alice.get("/api/auth/me").json()["tenant_id"]
    tb = bob.get("/api/auth/me").json()["tenant_id"]

    @scoped_activity
    async def read(args):
        with tx(args["agent_id"]) as c:
            assert {
                str(row["id"]) for row in all_rows(c, "SELECT id FROM lead.agents")
            } == {args["agent_id"]}
            return current_tenant()

    with tenant_scope(ta):
        assert await read({"agent_id": b}) == tb
        assert current_tenant() == ta
        with tx(b) as c:
            assert not all_rows(c, "SELECT id FROM lead.agents")
    assert agent_tenant(a) == ta


def test_public_attribution_uses_correct_workspace_without_owner_cookie(
    provider, monkeypatch
):
    from lead_app import analytics

    monkeypatch.setattr(analytics, "resolve_public", lambda *a: None)
    alice = provider.login("alice")
    bob = provider.login("bob")
    a = create(alice)
    b = create(bob)
    setup = bob.post(
        f"/api/agents/{b}/analytics", json={"site_url": "https://business.example/"}
    ).json()
    cid = setup["connection"]["id"]
    tracked = bob.post(
        f"/api/agents/{b}/tracking-links",
        json={"destination_url": "https://business.example/", "platform": "direct"},
    ).json()
    code = urlsplit(tracked["url"]).path.rsplit("/", 1)[-1]
    public = provider.client()
    sid = uid()
    assert public.get("/r/" + code, follow_redirects=False).status_code == 302
    r = public.post(
        "/collect/" + cid,
        headers={"Origin": "https://business.example"},
        json={
            "key": token("analytics:" + cid),
            "event": "pageview",
            "session_id": sid,
            "url": "https://business.example/",
            "ref": code,
        },
    )
    assert r.status_code == 204
    secret = bob.post(
        f"/api/agents/{b}/analytics/server-key", json={"connection_id": cid}
    ).json()["key"]
    server = TestClient(app)
    received = server.post(
        "/collect/server/conversion",
        headers={"Authorization": "Bearer " + secret},
        json={
            "connection_id": cid,
            "session_id": sid,
            "ref": code,
            "event_id": "signup:" + uid(),
            "type": "signup",
        },
    )
    assert received.status_code == 200
    assert bob.get(f"/api/agents/{b}").json()["metrics"]["conversions"] == 1
    assert alice.get(f"/api/agents/{a}").json()["metrics"]["conversions"] is None
    assert public.get("/api/agents/" + b).status_code == 401


def test_operator_owner_binding_requires_verified_active_session(provider):
    from lead_app.installation import bind_owner

    first = provider.login("binding-owner")
    second = provider.login("binding-new-owner")
    workspace = first.get("/api/auth/me").json()["tenant_id"]
    with pytest.raises(ValueError, match="有效"):
        bind_owner(workspace, provider.issuer, "unknown-person")
    result = bind_owner(workspace, provider.issuer, "binding-new-owner")
    assert result["owner_bound"]
    assert any(w["id"] == workspace for w in second.get("/api/workspaces").json())
