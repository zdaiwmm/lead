"""Third phase: sourced initialization, readiness and actual OpenAI wire contracts."""

import json
from datetime import datetime, timezone

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from lead_app import (
    admin_config,
    admin_content,
    admin_operations,
    admin_readiness,
    admin_settings,
    config,
    domain,
    model,
)
from lead_app import api as api_module
from lead_app.db import j, one, tx, uid
from lead_app.scope import tenant_scope
from lead_app.security import token
from openai import AsyncOpenAI
from pydantic import BaseModel

HEADERS = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}


@pytest.fixture
def workspace(monkeypatch):
    tenant = uid()
    with psycopg.connect(**config.database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'phase-three verification')",
            (tenant,),
        )
    monkeypatch.setattr(domain, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(admin_operations, "emit", lambda *a, **kw: None)
    with tenant_scope(
        tenant,
        {"tenant_id": tenant, "role": "owner", "issuer": "local", "subject": "local"},
    ):
        yield tenant
        with tx() as c:
            c.execute("UPDATE lead.agents SET desired_state='archived'")


def web():
    client = TestClient(api_module.app)
    client.cookies.set("lead_session", token("owner"))
    return client


def business_agent(url="https://seka.one", name="Seka"):
    aid = domain.create_agent()
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET name=%s WHERE id=%s", (name, aid))
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,product_url,description,source_refs) VALUES(lead.ctx_tenant(),%s,1,%s,'User supplied business',%s)",
            (aid, url, j([{"url": url}])),
        )
    return aid


def initialize(c):
    pack = admin_content.library(c)
    return admin_content.initialize(
        c,
        {entry["key"]: entry["version"] for entry in pack["entries"]},
        pack["content_digest"],
    )


def test_research_pack_publishes_33_plans_rich_onboarding_and_scoped_brand(workspace):
    aid = business_agent()
    other = business_agent("https://unrelated.example", "Another business")
    with tx() as c:
        result = initialize(c)
        assert len(result["applied"]) == 25
        assert (
            sum(
                len(admin_config.read(c, "strategies/" + p[0])["body"]["plans"])
                for p in domain.PLATFORMS
            )
            == 33
        )
        template = admin_config.onboarding_snapshot(c)
        assert template["version"] == 1
        assert all(
            field["help"] and field["placeholder"]
            for field in template["body"]["fields"].values()
        )
        assert template["body"]["fields"]["product_name"]["default"] == ""
        brand = admin_settings.brand_context(
            c, aid, now=datetime(2026, 10, 30, tzinfo=timezone.utc)
        )
        assert brand["name"] == "Seka" and len(brand["documents"]) == 6
        assert any(d["source"] == "https://seka.one/" for d in brand["documents"])
        custom = admin_config.read(c, "strategies/facebook/" + aid)["body"]
        assert "Seka" in custom["plans"][0]["template"]
        assert custom["plans"][0]["destination_path"].startswith("/")
        assert admin_settings.brand_context(c, other)["name"] == ""
        assert not any(
            "Seka" in d["content"]
            for d in admin_settings.brand_context(c, other)["documents"]
        )
        history = admin_config.history(c, "brand/" + aid)
        assert (
            history[0]["action"] == "publish"
            and admin_content.PACK_VERSION in history[0]["note"]
        )


def test_initialization_is_repeatable_and_preserves_custom_drafts(workspace):
    with tx() as c:
        draft = admin_content.onboarding_content()
        draft["fields"]["description"]["help"] = "Owner custom explanation"
        admin_config.write(c, "onboarding/default", draft, 0, action="draft")
        pack = admin_content.library(c)
        expected = {e["key"]: e["version"] for e in pack["entries"]}
        first = admin_content.initialize(c, expected, pack["content_digest"])
        assert "onboarding/default" in first["preserved"]
        assert (
            admin_config.read(c, "onboarding/default")["draft"]["fields"][
                "description"
            ]["help"]
            == "Owner custom explanation"
        )
        second = admin_content.initialize(c, expected, pack["content_digest"])
        assert not second["applied"] and len(second["preserved"]) == 13
        assert admin_config.read(c, "strategies/facebook")["version"] == 1
        assert admin_readiness.overview(c)["missing_templates"] == 1


def test_stale_pack_or_unmatched_brand_cannot_be_initialized(workspace):
    aid = business_agent("https://seka.one.evil.example")
    with tx() as c:
        pack = admin_content.library(c)
        assert "brand/" + aid not in {e["key"] for e in pack["entries"]}
        with pytest.raises(ValueError, match="已变化"):
            admin_content.initialize(c, {"onboarding/default": 0}, "0" * 64)
        with pytest.raises(ValueError, match="当前工作室"):
            admin_content.initialize(c, {"brand/" + aid: 0}, pack["content_digest"])
        assert not admin_config.history(c)


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_roles_can_preview_but_cannot_initialize(workspace, monkeypatch, role):
    from lead_app import identity_store

    monkeypatch.setattr(api_module, "auth_mode", lambda: "oidc")
    monkeypatch.setattr(
        identity_store,
        "lookup_session",
        lambda _: {
            "tenant_id": workspace,
            "role": role,
            "subject": "member",
            "issuer": "test",
        },
    )
    client = web()
    preview = client.get("/api/admin/content-library")
    assert preview.status_code == 200
    pack = preview.json()
    response = client.post(
        "/api/admin/content-library/initialize",
        headers=HEADERS,
        json={
            "content_digest": pack["content_digest"],
            "expected_versions": {"onboarding/default": 0},
        },
    )
    assert response.status_code == 403
    assert client.get("/api/admin/readiness").status_code == 200


def test_readiness_is_read_only_and_never_presents_missing_measurement_as_results(
    workspace,
):
    aid = business_agent()
    with tx() as c:
        before = admin_config.history(c)
        result = admin_readiness.overview(c)
        assert admin_config.history(c) == before
        checks = {v["code"]: v for v in result["agents"][0]["checks"]}
        assert checks["business"]["state"] == "ready"
        assert checks["brand"]["state"] == "needs_attention"
        assert checks["channels"]["state"] == "needs_attention"
        assert (
            checks["site"]["state"] == checks["conversion"]["state"] == "not_connected"
        )
        assert (
            not {"key", "secret_ref", "snippet"}
            & result["agents"][0]["models"]["chat"].keys()
        )
        initialize(c)
        result = admin_readiness.overview(
            c, now=datetime(2026, 10, 30, tzinfo=timezone.utc)
        )
        agent = next(a for a in result["agents"] if a["id"] == aid)
        assert (
            next(v for v in agent["checks"] if v["code"] == "brand")["state"] == "ready"
        )
        assert result["missing_templates"] == 0


def test_openai_model_catalog_and_legacy_references_are_unambiguous(monkeypatch):
    monkeypatch.setenv("LEAD_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("OPENAI_ALLOWED_MODELS", "gpt-5.4,gpt-4.1-mini")
    assert admin_settings.defaults("models")["default_model"] == "openai:gpt-4.1-mini"
    assert "openai:gpt-5.4" in admin_settings.model_catalog()
    assert config.resolve_model("deepseek-flash")["provider"] == "deepseek"
    assert config.resolve_model("openai:gpt-5.4")["provider"] == "openai"
    with pytest.raises(ValueError):
        config.resolve_model("unexpected:model")


class Answer(BaseModel):
    answer: str


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_model", ["gpt-4.1-mini", "gpt-5.4"])
async def test_openai_responses_uses_native_output_tools_limits_and_metering(
    workspace, monkeypatch, selected_model
):
    aid = domain.create_agent()
    monkeypatch.setenv("OPENAI_API_KEY", "verification-secret-never-saved")
    monkeypatch.setenv("OPENAI_ALLOWED_MODELS", selected_model)
    body = admin_settings.defaults("models")
    body["task_models"]["research"] = "openai:" + selected_model
    body.update(max_output_tokens=800, max_model_requests=2, max_tool_calls=1)
    with tx() as c:
        admin_config.write(c, "models/" + aid, body, 0)
    requests = []
    tool_calls = []

    async def lookup_fact() -> str:
        """Read the one supplied public fact."""
        tool_calls.append(True)
        return "A verified product fact"

    def respond(request):
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        requests.append(payload)
        output = (
            [
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup_fact",
                    "arguments": "{}",
                    "status": "completed",
                }
            ]
            if len(requests) == 1
            else [
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"answer":"Verified fact received"}',
                            "annotations": [],
                        }
                    ],
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "id": "resp_" + str(len(requests)),
                "object": "response",
                "created_at": 1793318400,
                "status": "completed",
                "model": selected_model,
                "output": output,
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 120,
                },
            },
        )

    transport = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(
        model,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(**kwargs, http_client=transport),
    )
    answer = await model.run_model(
        Answer,
        {"question": "Use lookup_fact, then report the result"},
        agent_id=aid,
        purpose="research",
        tools=[lookup_fact],
    )
    assert answer.answer == "Verified fact received" and tool_calls == [True]
    assert len(requests) == 2
    assert all(
        r["model"] == selected_model
        and r["store"] is False
        and r["max_output_tokens"] == 800
        for r in requests
    )
    assert requests[0]["text"]["format"]["type"] == "json_schema"
    assert all("thinking" not in r and "extra_body" not in r for r in requests)
    with tx(aid) as c:
        usage = one(
            c,
            "SELECT units,details FROM lead.usage_events ORDER BY created_at DESC LIMIT 1",
        )
        assert usage["units"] == 240
        assert usage["details"]["model"] == "openai:" + selected_model
        assert usage["details"]["provider"] == "openai"
        assert usage["details"]["cache_read_tokens"] == 40
        assert usage["details"]["cache_breakdown_verified"] is True
        assert "verification-secret" not in json.dumps(
            admin_config.history(c), default=str
        )


@pytest.mark.asyncio
async def test_missing_openai_key_does_not_fallback_or_reserve_tokens(
    workspace, monkeypatch
):
    aid = domain.create_agent()
    real_env = config.env
    monkeypatch.setattr(
        config,
        "env",
        lambda: {**real_env(), "OPENAI_API_KEY": "", "OPENAI_MODEL": "gpt-4.1-mini"},
    )
    body = admin_settings.defaults("models")
    body["default_model"] = "openai:gpt-4.1-mini"
    with tx() as c:
        admin_config.write(c, "models/" + aid, body, 0)
    with pytest.raises(model.ModelUnavailable):
        await model.run_model(Answer, "question", agent_id=aid)
    with tx(aid) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.budget_reservations")["n"] == 0
    response = web().post(
        "/api/admin/model-check?provider=openai", headers=HEADERS, json={}
    )
    assert response.status_code == 200 and response.json()["ok"] is False


def test_content_and_readiness_cannot_read_another_workspace(workspace):
    aid = business_agent()
    with tx() as c:
        original = admin_content.library(c)
    other = uid()
    with psycopg.connect(**config.database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'different phase-three workspace')",
            (other,),
        )
    with (
        tenant_scope(
            other,
            {
                "tenant_id": other,
                "role": "owner",
                "issuer": "local",
                "subject": "local",
            },
        ),
        tx() as c,
    ):
        pack = admin_content.library(c)
        assert pack["content_digest"] != original["content_digest"]
        assert "brand/" + aid not in {e["key"] for e in pack["entries"]}
        assert not admin_readiness.overview(c)["agents"]


def test_published_business_strategy_is_not_replaced_by_static_examples(
    workspace, monkeypatch
):
    from lead_app import operating_playbooks

    aid = business_agent()
    with tx() as c:
        initialize(c)
        key = "strategies/facebook/" + aid
        current = admin_config.read(c, key)
        current["body"]["plans"][0]["first_batch"] = ["Owner's new research topic"]
        current["body"]["plans"][0]["template"] = "Owner's verified template"
        admin_config.write(c, key, current["body"], current["version"])
        monkeypatch.setattr(
            operating_playbooks, "evidence", lambda *args: ([], "not_ready")
        )
        target = uid()
        context = operating_playbooks.planning_context(
            c,
            [
                {
                    "platform": "facebook",
                    "target_id": target,
                    "available_formats": ["text"],
                }
            ],
            {
                "id": uid(),
                "agent_id": aid,
                "product_url": "https://seka.one",
                "facts": {},
            },
            1,
        )[target]
        assert context["selected_plan"]["first_batch"] == ["Owner's new research topic"]
        assert context["selected_plan"]["template"] == "Owner's verified template"
        assert context["destination_url"] == "https://seka.one/translate"
        bad = current["body"]
        bad["plans"][0]["destination_path"] = "//outside.example/path"
        with pytest.raises(ValueError, match="承接路径"):
            admin_config.validate(key, bad)
