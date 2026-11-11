"""Adapters retain shared checkpoints, recovery isolation, and HTTP boundaries."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from lead_app import (
    admin_settings,
    browser,
    community_discovery,
    community_growth,
    community_health,
    community_runtime,
    domain,
    model,
)
from lead_app.db import j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.mark.asyncio
async def test_adapter_step_uses_requested_profile_and_shared_timing(monkeypatch):
    aid, profile, task = str(uuid4()), str(uuid4()), uuid4()
    actor = SimpleNamespace(profile={"id": profile}, session=uuid4())
    selected, calls, writes = [], [], []

    async def get(agent, profile_id=None):
        selected.append((agent, profile_id))
        return actor

    async def adapter_step(actual, task_id, operation):
        calls.append((actual, task_id, operation))
        return {"state": "confirmed", "receipt": "original"}

    @contextmanager
    def fake_tx(_):
        yield SimpleNamespace(execute=lambda sql, values: writes.append(values))

    monkeypatch.setattr(browser.gateway, "get", get)
    monkeypatch.setattr(browser, "tx", fake_tx)
    result = await browser.execute_community_step(
        aid,
        browser.CommunityStepRequest(task_id=task, operation="receipt"),
        profile_id=profile,
        step=adapter_step,
    )
    assert result == {"state": "confirmed", "receipt": "original"}
    assert selected == [(aid, profile)]
    assert calls == [(actor, str(task), "receipt")]
    assert len(writes) == 1
    timing = writes[0][0].obj["browser_timing"]
    assert timing["operation"] == "receipt"
    assert timing["cold_start"] is True
    assert timing["duration_ms"] >= timing["gateway_ms"] >= 0


def insert_task(aid, task_type, payload, saved):
    with tx(aid) as c:
        return one(
            c,
            """INSERT INTO lead.tasks
            (tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
            VALUES(%s,%s,%s,'running',%s,%s,%s,now()) RETURNING *""",
            (
                current_tenant(),
                aid,
                task_type,
                "fixture:" + uid(),
                j(payload),
                j(saved),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("local_timeout", [False, True])
async def test_discovery_local_model_policy_reaches_assessment_without_provider_fallback(
    monkeypatch, local_timeout
):
    # Losing the override at either shared boundary would call the provider.
    monkeypatch.setattr(community_growth, "emit", lambda *a, **k: None)
    monkeypatch.setattr(admin_settings, "tool_enabled", lambda *a: True)
    aid = domain.create_agent()
    community_growth.set_strategy(aid, {"enabled": True})
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        agent = one(c, "SELECT * FROM lead.agents")
        c.execute(
            """INSERT INTO lead.business_profiles
            (tenant_id,agent_id,revision,description,facts,source_refs)
            VALUES(%s,%s,1,'EPUB reading','{}','[]')""",
            (current_tenant(), aid),
        )
    observed = {
        "name": "Reading fixture",
        "telegram_url": "https://t.me/readers_group",
        "source_url": "https://example.org",
        "page_excerpt": "Reading discussions",
    }
    task = insert_task(
        aid,
        "community.discover",
        {
            "checkpoint_version": "2",
            "control_version": agent["control_version"],
            "config_revision": agent["config_revision"],
        },
        {
            "phase": "assess",
            "cursor": 0,
            "queue": list(community_discovery.SEEDS),
            "link_extractor_version": 5,
            "sources": [],
            "assessed_urls": [],
            "errors": [],
            "observed_candidates": [observed],
        },
    )
    local_calls, provider_calls = [], []

    async def provider(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError(
            "Private model inputs must never reach the default provider"
        )

    async def local(result_type, prompt, **kwargs):
        local_calls.append((result_type, prompt, kwargs))
        if local_timeout:
            raise TimeoutError("local model unavailable")
        return community_growth.Discovery(candidates=[], summary="Local assessment")

    monkeypatch.setattr(model, "run_model", provider)
    result = await community_growth.discover_communities(aid, invoke_model=local)
    assert result["reason"] == "community_discovery_progress"
    assert provider_calls == []
    assert len(local_calls) == 1
    result_type, prompt, options = local_calls[0]
    assert result_type is community_growth.Discovery
    assert prompt["observed_links"] == [observed]
    assert options["agent_id"] == aid and options["short_request"] is True
    with tx(aid) as c:
        saved = one(c, "SELECT state,result FROM lead.tasks WHERE id=%s", (task["id"],))
    assert saved["state"] == "running"
    assert saved["result"]["observed_candidates"] == [observed]
    assert saved["result"]["execution_steps_count"] == 1
    if local_timeout:
        assert saved["result"]["phase"] == "assess"
        assert saved["result"]["model_attempts"] == 1
        assert saved["result"]["assessed_urls"] == []
    else:
        assert saved["result"]["phase"] == "collect"
        assert saved["result"]["model_attempts"] == 0
        assert saved["result"]["assessed_urls"] == ["https://t.me/readers_group"]


class BrowserActor:
    """No browser process: retain the gateway's actual lock and close lifecycle."""

    def __init__(self, aid, profile, slot="telegram"):
        self.aid = aid
        self.profile = {"id": profile, "slot": slot}
        self.lock = asyncio.Lock()
        self.page = self
        self.closed = False

    async def evaluate(self, script):
        return {"bodyClass": "fixture"}

    async def close(self):
        self.closed = True


def browser_fixture(monkeypatch):
    aid, profile = domain.create_agent(), uid()
    gateway = browser.Gateway()
    original = BrowserActor(aid, profile)
    unrelated = BrowserActor(aid, uid(), "reddit")
    gateway.actors = {profile: original, unrelated.profile["id"]: unrelated}
    selected = []

    async def acquire(agent, profile_id=None):
        selected.append((agent, profile_id))
        assert agent == aid and profile_id == profile
        if profile not in gateway.actors:
            gateway.actors[profile] = BrowserActor(aid, profile)
        return gateway.actors[profile]

    monkeypatch.setattr(gateway, "get", acquire)
    monkeypatch.setattr(browser, "gateway", gateway)
    task = insert_task(
        aid,
        "community.engage",
        {
            "external_account_id": "fixture-account",
            "candidate": {"telegram_url": "https://t.me/readers_group"},
        },
        {"target_id": "fixture-peer"},
    )
    return aid, profile, gateway, original, unrelated, selected, task


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["refresh", "rebuild_profile"])
async def test_shared_recovery_keeps_adapter_and_only_restarts_selected_profile(
    monkeypatch, recovery
):
    aid, profile, gateway, original, unrelated, selected, task = browser_fixture(
        monkeypatch
    )
    if recovery == "rebuild_profile":
        with tx(aid) as c:
            community_health.failed(
                c, aid, task, community_health.fault(TimeoutError(), "receipt")
            )
            assert community_health.claim_recovery(c, aid, task, "refresh")
    attempts, refreshed = [], []

    async def adapter_step(actor, task_id, operation):
        attempts.append(actor)
        assert task_id == str(task["id"]) and operation == "receipt"
        if len(attempts) == 1:
            raise TimeoutError("read timed out")
        return {"state": "confirmed", "receipt": "original"}

    async def adapter_reload(page, account):
        refreshed.append((page, account))

    async def forbidden_default(*args, **kwargs):
        raise AssertionError("Recovery must retain the selected adapter")

    monkeypatch.setattr(community_runtime, "browser_step", forbidden_default)
    monkeypatch.setattr(community_runtime, "reload_telegram", forbidden_default)
    result = await browser.execute_community_step(
        aid,
        browser.CommunityStepRequest(task_id=task["id"], operation="receipt"),
        profile_id=profile,
        step=adapter_step,
        reload=adapter_reload,
    )
    assert result == {"state": "confirmed", "receipt": "original"}
    assert len(attempts) == 2
    assert gateway.actors[unrelated.profile["id"]] is unrelated
    assert unrelated.closed is False
    assert attempts[0] is original and attempts[1] is gateway.actors[profile]
    if recovery == "rebuild_profile":
        assert original.closed is True and attempts[1] is not original
        assert selected == [(aid, profile), (aid, profile)]
        assert refreshed == []
    else:
        assert original.closed is False and attempts[1] is original
        assert selected == [(aid, profile)]
        assert refreshed == [(original.page, "fixture-account")]
    with tx(aid) as c:
        health = community_health.get(c, aid, task)
    assert health["recovery_actions"].get(recovery)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reply", "join"])
@pytest.mark.parametrize("write_state", ["dispatching", "unknown"])
async def test_uncertain_submit_preserves_evidence_and_never_retries(
    monkeypatch, operation, write_state
):
    aid, profile, gateway, original, unrelated, selected, task = browser_fixture(
        monkeypatch
    )
    saved = {
        "target_id": "fixture-peer",
        operation + "_state": write_state,
        "original_receipt": "preserve-this",
    }
    with tx(aid) as c:
        c.execute("UPDATE lead.tasks SET result=%s WHERE id=%s", (j(saved), task["id"]))
    attempts = []

    async def adapter_step(actor, task_id, actual_operation):
        attempts.append((actor, task_id, actual_operation))
        raise TimeoutError("submit acknowledgement lost")

    async def forbidden_reload(*args):
        raise AssertionError("An uncertain write must not be retried")

    with pytest.raises(HTTPException) as caught:
        await browser.execute_community_step(
            aid,
            browser.CommunityStepRequest(task_id=task["id"], operation=operation),
            profile_id=profile,
            step=adapter_step,
            reload=forbidden_reload,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["write_may_have_happened"] is True
    assert attempts == [(original, str(task["id"]), operation)]
    assert selected == [(aid, profile)]
    assert not original.closed and not unrelated.closed
    with tx(aid) as c:
        current = one(c, "SELECT result FROM lead.tasks WHERE id=%s", (task["id"],))[
            "result"
        ]
        assert community_health.get(c, aid, task) is None
    assert current[operation + "_state"] == write_state
    assert current["original_receipt"] == "preserve-this"
    assert current["fault"]["write_may_have_happened"] is True


@pytest.mark.asyncio
async def test_http_route_cannot_select_callbacks_or_profile(monkeypatch):
    aid = domain.create_agent()
    task = insert_task(aid, "community.engage", {}, {})
    actor = BrowserActor(aid, uid(), "default")
    selected, calls = [], []

    async def get(agent, profile_id=None):
        selected.append((agent, profile_id))
        return actor

    async def default_step(actual, task_id, operation):
        calls.append((actual, task_id, operation))
        return {"state": "confirmed", "receipt": "default-adapter"}

    monkeypatch.setattr(browser.gateway, "get", get)
    monkeypatch.setattr(community_runtime, "browser_step", default_step)
    monkeypatch.setattr(browser, "internal_token", lambda: "fixture-token")
    injections = {
        "step": "arbitrary.module:callback",
        "reload": "arbitrary.reload",
        "profile_id": uid(),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=browser.app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            f"/agents/{aid}/community/step",
            params=injections,
            json={"task_id": str(task["id"]), "operation": "receipt", **injections},
            headers={"authorization": "Bearer fixture-token"},
        )
    assert response.status_code == 200
    assert response.json() == {"state": "confirmed", "receipt": "default-adapter"}
    assert selected == [(aid, None)]
    assert calls == [(actor, str(task["id"]), "receipt")]


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", [False, True])
@pytest.mark.parametrize("flag", [None, "0", "true", "1"])
async def test_browser_launch_main_world_eval_requires_explicit_operator_setting(
    monkeypatch, namespace, flag
):
    from lead_app import managed_network, network_namespace, resources

    aid = domain.create_agent()
    settings = {} if flag is None else {"LEAD_BROWSER_MAIN_WORLD_EVAL": flag}
    captured = {}

    class Proxy:
        browser_proxy = {"server": "http://fixture.invalid:1"}
        bytes_forwarded = 0
        connections = 0

        async def start(self):
            return self

        async def close(self):
            captured["proxy_closed"] = True

    class Context:
        pages = [object()]

        def on(self, event, callback):
            pass

        async def route(self, pattern, handler):
            pass

        async def add_cookies(self, cookies):
            pass

    class Manager:
        def __init__(self, **kwargs):
            captured["manager"] = kwargs

        async def __aenter__(self):
            return Context()

        async def __aexit__(self, *args):
            captured["browser_closed"] = True

    prepared = {"executable_path": "fixture-engine"}
    wrapped = {"executable_path": "fixture-namespace"}

    def prepare(**kwargs):
        captured["launch_options"] = kwargs
        return prepared

    def apply(options, agent, proxy, storage):
        assert options is prepared and agent == aid
        captured["namespace"] = True
        return wrapped

    monkeypatch.setattr(browser, "env", lambda: settings)
    monkeypatch.setattr(resources, "ensure_capacity", lambda *args: None)
    monkeypatch.setattr(browser.reddit_login, "assign_exit", lambda *args: None)
    monkeypatch.setattr(browser, "proxy_for", lambda *args: None)
    monkeypatch.setattr(managed_network, "policy", lambda *args: {})
    monkeypatch.setattr(
        browser, "generate_fingerprint", lambda **kwargs: {"os": "linux"}
    )
    monkeypatch.setattr(browser, "BrowserForwardProxy", lambda *args: Proxy())
    monkeypatch.setattr(browser, "AsyncCamoufox", Manager)
    monkeypatch.setattr(browser, "launch_options", prepare)
    monkeypatch.setattr(network_namespace, "required", lambda: namespace)
    monkeypatch.setattr(network_namespace, "apply", apply)
    actor = browser.Actor(aid)
    try:
        await actor.launch()
        with tx(aid) as c:
            assert (
                one(
                    c,
                    "SELECT state FROM lead.browser_sessions WHERE id=%s",
                    (actor.session,),
                )["state"]
                == "ready"
            )
        if namespace:
            assert captured["namespace"] is True
            assert captured["manager"] == {
                "persistent_context": True,
                "from_options": wrapped,
            }
            effective = captured["launch_options"]
        else:
            assert "launch_options" not in captured and "namespace" not in captured
            assert captured["manager"]["persistent_context"] is True
            effective = captured["manager"]
        if flag == "1":
            assert effective["main_world_eval"] is True
        else:
            assert "main_world_eval" not in effective
    finally:
        await actor.close()
    assert captured["proxy_closed"] and captured["browser_closed"]
