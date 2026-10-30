"""Health must describe published task models, without calling a provider."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from lead_app import admin_settings, api, config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credentials,task_models,expected",
    [
        ({"OPENAI_API_KEY": "fixture-openai"}, {}, True),
        ({"DEEPSEEK_API_KEY": "fixture-deepseek"}, {}, False),
        (
            {"OPENAI_API_KEY": "fixture-openai"},
            {"research": "deepseek:missing-research"},
            False,
        ),
    ],
)
async def test_health_uses_workspace_models_instead_of_deployment_default(
    monkeypatch, tmp_path, credentials, task_models, expected
):
    values = {"LEAD_MODEL_PROVIDER": "deepseek", **credentials}
    monkeypatch.setattr(config, "env", lambda: values)
    monkeypatch.setattr(api, "env", lambda: values)
    monkeypatch.setattr(api, "STATE", tmp_path)
    (tmp_path / "worker-heartbeat").touch()
    settings = {
        "default_model": "openai:workspace-model",
        "task_models": task_models,
    }

    @contextmanager
    def database():
        yield object()

    def effective(c, kind, aid):
        assert kind == "models" and aid is None
        return settings, "models/default@2"

    async def gateway(*args, **kwargs):
        return SimpleNamespace(json=lambda: {"ok": True, "active_profiles": 1})

    monkeypatch.setattr(api, "tx", database)
    monkeypatch.setattr(api, "one", lambda *args: {"ok": 1})
    monkeypatch.setattr(api, "gateway", gateway)
    monkeypatch.setattr(admin_settings, "effective", effective)
    result = await api.health()
    assert result["database"] and result["worker"] and result["browser"]
    assert result["model_configured"] is expected
    assert result["model"] == "workspace-model"
    assert result["models"]["planning"] == {
        "reference": "openai:workspace-model",
        "version": "models/default@2",
        "credential_configured": bool(credentials.get("OPENAI_API_KEY")),
    }
    assert result["models"]["research"]["reference"] == task_models.get(
        "research", "openai:workspace-model"
    )
    assert all(secret not in str(result) for secret in credentials.values())


@pytest.mark.asyncio
async def test_database_outage_keeps_health_available_with_deployment_fallback(
    monkeypatch, tmp_path
):
    values = {"LEAD_MODEL_PROVIDER": "deepseek", "DEEPSEEK_MODEL": "fallback"}
    monkeypatch.setattr(config, "env", lambda: values)
    monkeypatch.setattr(api, "env", lambda: values)
    monkeypatch.setattr(api, "STATE", tmp_path)

    @contextmanager
    def unavailable():
        raise ConnectionError("fixture database is offline")
        yield

    async def gateway(*args, **kwargs):
        raise ConnectionError("fixture browser is offline")

    monkeypatch.setattr(api, "tx", unavailable)
    monkeypatch.setattr(api, "gateway", gateway)
    result = await api.health()
    assert not result["database"] and not result["browser"]
    assert result["model"] == "fallback"
    assert result["model_configured"] is False
    assert result["models"] == {}
