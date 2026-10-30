"""A broken workspace must not flood connections or delay healthy work."""

import asyncio
from types import SimpleNamespace

import pytest
from lead_app import worker


@pytest.mark.asyncio
async def test_failed_workspace_backs_off_while_healthy_workspace_keeps_delivering(
    monkeypatch, tmp_path
):
    now = [0]
    calls = []

    async def tick(seconds):
        now[0] += seconds
        if now[0] >= 12:
            raise asyncio.CancelledError

    async def deliver(client, tenant, configured, was_configured):
        calls.append((tenant, now[0], was_configured))
        if tenant == "broken":
            raise ConnectionError("fixture unavailable")

    monkeypatch.setattr(worker, "STATE", tmp_path)
    monkeypatch.setattr(
        worker, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0])
    )
    monkeypatch.setattr(
        worker,
        "asyncio",
        SimpleNamespace(
            sleep=tick,
            to_thread=asyncio.to_thread,
            Semaphore=asyncio.Semaphore,
            create_task=asyncio.create_task,
        ),
    )
    monkeypatch.setattr(worker, "execution_tenants", lambda: ["broken", "healthy"])
    monkeypatch.setattr(worker, "any_model_configured", lambda: True)
    monkeypatch.setattr(worker, "model_config", lambda provider: {"key": "fixture"})
    monkeypatch.setattr(worker, "deliver_workspace", deliver)
    with pytest.raises(asyncio.CancelledError):
        await worker.publish_outbox(None)
    assert [at for tenant, at, _ in calls if tenant == "broken"] == [0, 2, 6]
    assert [at for tenant, at, _ in calls if tenant == "healthy"] == list(range(12))
    assert [configured for tenant, _, configured in calls if tenant == "healthy"] == [
        False
    ] + [True] * 11


@pytest.mark.asyncio
async def test_notification_failure_does_not_rebootstrap_successful_outbox(
    monkeypatch, tmp_path
):
    from lead_app import admin_notifications

    now = [30]
    configured_calls = []

    async def tick(seconds):
        now[0] += seconds
        if now[0] >= 40:
            raise asyncio.CancelledError

    async def deliver(client, tenant, configured, was_configured):
        configured_calls.append(was_configured)

    def unavailable():
        raise ConnectionError("fixture notification collection")

    monkeypatch.setattr(worker, "STATE", tmp_path)
    monkeypatch.setattr(
        worker, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0])
    )
    monkeypatch.setattr(
        worker,
        "asyncio",
        SimpleNamespace(
            sleep=tick,
            to_thread=asyncio.to_thread,
            Semaphore=asyncio.Semaphore,
            create_task=asyncio.create_task,
        ),
    )
    monkeypatch.setattr(worker, "execution_tenants", lambda: ["fixture"])
    monkeypatch.setattr(worker, "any_model_configured", lambda: True)
    monkeypatch.setattr(worker, "model_config", lambda provider: {"key": "fixture"})
    monkeypatch.setattr(worker, "deliver_workspace", deliver)
    monkeypatch.setattr(admin_notifications, "collect", unavailable)
    with pytest.raises(asyncio.CancelledError):
        await worker.publish_outbox(None)
    assert configured_calls == [False, True, True]
