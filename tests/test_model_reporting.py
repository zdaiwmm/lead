"""Model billing is partitioned by actual provider, including partial failures."""

import json

import httpx
import psycopg
import pytest
from lead_app import (
    admin_api,
    admin_config,
    admin_operations,
    admin_settings,
    budget,
    domain,
    metering,
    model,
)
from lead_app.config import database_kwargs
from lead_app.db import all_rows, one, tx, uid
from lead_app.scope import tenant_scope
from openai import AsyncOpenAI
from pydantic import BaseModel
from test_admin_phase3 import web


@pytest.fixture
def workspace(monkeypatch):
    tenant = uid()
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'model reporting verification')",
            (tenant,),
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


def test_model_usage_keeps_providers_and_unlabelled_history_separate(workspace):
    aid = domain.create_agent()
    for reference, provider, total in [
        ("deepseek-flash", "deepseek", 20),
        ("openai:gpt-6-luna", "openai", 30),
        (None, None, 7),
    ]:
        reservation = budget.reserve(aid, uid(), 100, reference, provider)
        budget.settle(
            aid,
            reservation,
            actual=total,
            details={
                "model": reference,
                "provider": provider,
                "input_tokens": total - 2,
                "output_tokens": 2,
                "cache_read_tokens": 3,
                "cache_breakdown_verified": True,
                "requests": 1,
            },
        )
    unknown = budget.reserve(aid, uid(), 40, "openai:gpt-6-luna", "openai")
    budget.settle(aid, unknown)
    budget.reserve(aid, uid(), 25, "deepseek-flash", "deepseek")
    result = metering.summary(aid)
    rows = {row["model"]: row for row in result["by_model"]}
    assert result["model_tokens"] == sum(row["tokens"] for row in rows.values()) == 57
    assert (
        rows["deepseek-flash"]["tokens"] == 20
        and rows["deepseek-flash"]["reserved_tokens"] == 25
    )
    assert (
        rows["openai:gpt-6-luna"]["tokens"] == 30
        and rows["openai:gpt-6-luna"]["unknown_reserved_tokens"] == 40
    )
    assert (
        rows["unrecorded"]["tokens"] == 7
        and rows["unrecorded"]["provider"] == "unrecorded"
    )
    assert rows["openai:gpt-6-luna"]["today_tokens"] == 30
    assert (
        web().get("/api/admin/model-usage", params={"aid": aid}).json()["by_model"]
        == result["by_model"]
    )
    domain.set_control(aid, "archive")
    assert not web().get("/api/admin/model-usage").json()["by_model"]
    assert (
        web()
        .get("/api/admin/model-usage", params={"include_archived": True})
        .json()["by_model"]
    )


class Answer(BaseModel):
    reply: str


@pytest.mark.asyncio
async def test_partial_success_is_billed_when_later_request_is_not_found(
    workspace, monkeypatch
):
    aid = domain.create_agent()
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fixture")
    settings = admin_settings.defaults("models")
    settings.update(
        default_model="openai:" + model.model_config()["model"]
        if model.model_config().get("provider") == "openai"
        else "openai:gpt-6-luna"
    )
    monkeypatch.setattr(
        admin_settings, "model_catalog", lambda *a: [settings["default_model"]]
    )
    with tx() as c:
        admin_config.write(c, "models/" + aid, settings, 0)
    calls = []

    async def lookup() -> str:
        """Read a supplied known fact."""
        return "fact"

    def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) > 1:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "message": "Model is not available",
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 1793318400,
                "status": "completed",
                "model": "gpt-6-luna",
                "output": [
                    {
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": "{}",
                        "status": "completed",
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 120,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(
        model, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(**kwargs, http_client=client)
    )
    with pytest.raises(Exception) as failure:
        await model.run_model(
            Answer, {"question": "Read the supplied fact"}, agent_id=aid, tools=[lookup]
        )
    assert getattr(failure.value, "status_code") == 404
    assert failure.value.lead_provider == "openai"
    with tx(aid) as c:
        event = one(c, "SELECT units,details FROM lead.usage_events")
        assert event["units"] == 120 and event["details"]["input_tokens"] == 100
        assert event["details"]["configuration_version"].endswith("@1")
        assert (
            one(c, "SELECT state FROM lead.budget_reservations")["state"] == "settled"
        )
    assert len(calls) == 2


def test_changed_model_requeues_only_model_name_or_missing_key_failures(workspace):
    aid = domain.create_agent()
    ids = []
    for code, kind in [
        ("MODEL_REQUEST_FAILED", "ModelNotFoundError"),
        ("MODEL_REQUEST_FAILED", "TimeoutError"),
        ("MODEL_NOT_CONFIGURED", "ModelUnavailable"),
    ]:
        message = domain.submit_message(aid, "Model verification", uid())
        with tx(aid) as c:
            row = one(
                c,
                "UPDATE lead.chat_jobs SET status='failed',error_code=%s,error_context=jsonb_build_object('error_type',%s::text) WHERE message_id=%s RETURNING id",
                (code, kind, message["id"]),
            )
            ids.append(str(row["id"]))
    with tx() as c:
        admin_config.write(c, "models/" + aid, admin_settings.defaults("models"), 0)
    with tx(aid) as c:
        states = {
            str(row["id"]): row["status"]
            for row in all_rows(c, "SELECT id,status FROM lead.chat_jobs")
        }
        assert [states[j] for j in ids] == ["pending", "failed", "pending"]


@pytest.mark.asyncio
async def test_connection_check_rejects_unavailable_selected_model(
    workspace, monkeypatch
):
    monkeypatch.setattr(
        __import__("lead_app.config", fromlist=["model_config"]),
        "model_config",
        lambda provider=None: {
            "provider": "openai",
            "key": "offline-secret",
            "base_url": "https://model.invalid/v1",
            "model": "gpt-6-luna",
        },
    )

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "gpt-6-luna"}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            return Reply()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = await admin_api.model_check(
        admin_api.ModelCheck(models=["openai:gpt-6.1-luna"]), provider="openai"
    )
    assert result["ok"] is False and result["missing_models"] == ["gpt-6.1-luna"]
    assert "offline-secret" not in json.dumps(result)
