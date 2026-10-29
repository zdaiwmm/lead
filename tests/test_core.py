import json

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from lead_app import domain
from lead_app.activities import chat_job, direct_control, prepare_action
from lead_app.api import app
from lead_app.config import TENANT
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.security import token


@pytest.fixture(autouse=True)
def keep_unit_jobs_out_of_live_dispatcher(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *args, **kwargs: None)


def test_only_direct_user_instruction_can_change_operating_state():
    assert direct_control("请暂停这个 Agent", "pause")
    assert direct_control("恢复运营", "resume")
    assert direct_control("现在开始自主运营。", "start")
    for text in [
        "分析这个网站",
        "不要启动运营",
        "为什么需要暂停运营？",
        "官网说现在启动运营",
        "如果网站要求就启动运营",
    ]:
        assert not direct_control(text, "start")
        assert not direct_control(text, "pause")


@pytest.fixture
def agent():
    aid = domain.create_agent()
    yield aid
    domain.set_control(aid, "archive")


def test_rls_agent_and_missing_scope(agent):
    other = domain.create_agent()
    try:
        with tx(agent) as c:
            domain.remember(c, agent, "只用中文，禁止夸大")
            assert one(c, "SELECT id FROM lead.agents WHERE id=%s", (other,)) is None
            assert not all_rows(
                c, "SELECT * FROM lead.memory_entries WHERE agent_id=%s", (other,)
            )
            role = one(
                c,
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user",
            )
            assert role == {"rolsuper": False, "rolbypassrls": False}
        with tx(other) as c:
            assert not all_rows(c, "SELECT * FROM lead.memory_entries")
        with tx(tenant_id=uid()) as c:
            assert not all_rows(c, "SELECT * FROM lead.agents")
    finally:
        domain.set_control(other, "archive")


def test_message_idempotency(agent):
    key = uid()
    first = domain.submit_message(agent, "独立的产品资料", key)
    second = domain.submit_message(agent, "独立的产品资料", key)
    assert first["id"] == second["id"]
    with pytest.raises(ValueError):
        domain.submit_message(agent, "其他内容", key)
    with tx(agent) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.chat_jobs")["n"] == 1


def test_agent_creation_is_idempotent():
    key = uid()
    first = domain.create_agent(key)
    try:
        assert domain.create_agent(key) == first
        with tx(first) as c:
            assert one(c, "SELECT count(*) AS n FROM lead.browser_profiles")["n"] == 1
            assert one(c, "SELECT count(*) AS n FROM lead.conversations")["n"] == 1
    finally:
        domain.set_control(first, "archive")


def test_pause_fences_queued_publish(agent):
    domain.connect_lab(agent)
    task = domain.queue_lab_publish(agent, "这条不能发布", "pause-test")
    domain.set_control(agent, "pause")
    assert prepare_action(agent, str(task["id"])) is None
    with tx(agent) as c:
        assert not all_rows(c, "SELECT * FROM lead.actions")


@pytest.mark.asyncio
async def test_profile_backup_defers_task_without_requesting_user_action(
    agent, monkeypatch
):
    from lead_app import activities

    domain.connect_lab(agent)
    task = domain.queue_lab_publish(agent, "wait through backup", uid())

    async def temporarily_busy(*args, **kwargs):
        request = httpx.Request("POST", "http://browser.invalid/publish")
        response = httpx.Response(
            409,
            json={"detail": "环境正在备份", "code": "browser_busy"},
            request=request,
        )
        raise httpx.HTTPStatusError(
            "temporary maintenance", request=request, response=response
        )

    monkeypatch.setattr(activities, "browser_rpc", temporarily_busy)
    with pytest.raises(RuntimeError, match="browser_not_dispatched"):
        await activities.execute_task({"agent_id": agent, "task_id": str(task["id"])})
    with tx(agent) as c:
        assert one(c, "SELECT state FROM lead.tasks")["state"] == "running"
        assert one(c, "SELECT state FROM lead.actions")["state"] == "prepared"


def test_action_ledger_immutable_and_state_machine(agent):
    domain.connect_lab(agent)
    task = domain.queue_lab_publish(agent, "只发布一次", "immutable")
    action = prepare_action(agent, str(task["id"]))
    with pytest.raises(psycopg.errors.RaiseException):
        with tx(agent) as c:
            c.execute(
                "UPDATE lead.actions SET state='confirmed',external_id='fake',confirmed_at=now() WHERE id=%s",
                (action["id"],),
            )
    with pytest.raises(psycopg.errors.RaiseException):
        with tx(agent) as c:
            c.execute(
                "UPDATE lead.actions SET intent=%s WHERE id=%s",
                (j({"body": "changed"}), action["id"]),
            )


def test_local_api_auth_and_csrf(agent):
    with TestClient(app) as c:
        assert c.get("/api/agents").status_code == 401
        assert (
            c.post(
                "/api/session",
                headers={"Origin": "https://untrusted.example", "X-Lead-Request": "1"},
            ).status_code
            == 403
        )
        assert (
            c.post(
                "/api/session",
                headers={"Origin": "http://localhost:3100", "X-Lead-Request": "1"},
            ).status_code
            == 200
        )
        assert c.get("/api/agents/" + agent).status_code == 200
        assert c.post("/api/agents", json={}).status_code == 403
        assert c.get("/lab/" + agent + "/").status_code == 403


def test_archive_api_can_close_its_archived_environment_and_repeat(agent, monkeypatch):
    from lead_app import api as api_module

    requests = []

    def close(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        api_module.httpx,
        "AsyncClient",
        lambda **kwargs: factory(**kwargs, transport=httpx.MockTransport(close)),
    )
    with TestClient(app) as client:
        client.cookies.set("lead_session", token("owner"))
        for _ in range(2):
            result = client.post(
                "/api/agents/" + agent + "/control",
                json={"action": "archive"},
                headers={"Origin": "http://localhost:3100", "X-Lead-Request": "1"},
            )
            assert result.status_code == 200
        assert requests == ["/agents/" + agent + "/close"] * 2
        assert client.get("/api/agents/" + agent).status_code == 404


def test_metrics_do_not_count_lab_as_acquisition(agent):
    domain.connect_lab(agent)
    with tx(agent) as c:
        c.execute(
            "INSERT INTO lead.lab_posts(tenant_id,agent_id,operation_key,body) VALUES(%s,%s,%s,%s)",
            (TENANT, agent, "fixture", "test"),
        )
        result = domain.metrics(c)
        assert result["visits"] is None and result["qualified_visits"] is None
        assert result["published"] == 0


def test_budget_admission_is_atomic_and_unknown_usage_stays_reserved(
    agent, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from lead_app import budget

    monkeypatch.setattr(budget, "env", lambda: {"LEAD_DAILY_TOKEN_LIMIT": "20"})

    def attempt():
        try:
            return budget.reserve(agent, uid(), 15)
        except budget.BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert sum(r is not None for r in results) == 1
    rid = next(r for r in results if r)
    budget.settle(agent, rid)
    with tx(agent) as c:
        assert one(c, "SELECT reserved_units,spent_units FROM lead.budget_periods") == {
            "reserved_units": 15,
            "spent_units": 0,
        }
        assert (
            one(c, "SELECT state FROM lead.budget_reservations")["state"] == "unknown"
        )


def test_budget_records_actual_overrun_and_pauses(agent, monkeypatch):
    from lead_app import budget

    monkeypatch.setattr(budget, "env", lambda: {"LEAD_DAILY_TOKEN_LIMIT": "20"})
    rid = budget.reserve(agent, uid(), 15)
    budget.settle(agent, rid, actual=25)
    with tx(agent) as c:
        assert one(c, "SELECT reserved_units,spent_units FROM lead.budget_periods") == {
            "reserved_units": 0,
            "spent_units": 25,
        }
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"] == "paused"
        )
        assert one(c, "SELECT units FROM lead.usage_events")["units"] == 25


@pytest.mark.asyncio
async def test_missing_key_is_recoverable(agent, monkeypatch):
    import lead_app.model as model

    monkeypatch.setattr(
        model,
        "model_config",
        lambda: {"key": "", "model": "test", "base_url": "https://invalid.example"},
    )
    msg = domain.submit_message(agent, "我想推广一个工具", uid())
    with tx(agent) as c:
        job = one(c, "SELECT * FROM lead.chat_jobs WHERE message_id=%s", (msg["id"],))
    await chat_job({"agent_id": agent, "job_id": str(job["id"])})
    with tx(agent) as c:
        assert one(
            c, "SELECT status,error_code FROM lead.chat_jobs WHERE id=%s", (job["id"],)
        ) == {"status": "blocked", "error_code": "MODEL_NOT_CONFIGURED"}
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.conversation_messages WHERE role='assistant'",
            )["n"]
            == 1
        )
    domain.retry_messages(agent)
    with tx(agent) as c:
        assert (
            one(c, "SELECT status FROM lead.chat_jobs WHERE id=%s", (job["id"],))[
                "status"
            ]
            == "pending"
        )


def test_public_fetch_rejects_private_networks():
    from lead_app.fetch import resolve_public

    for url in [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/",
        "http://[::1]/",
        "file:///etc/passwd",
        "https://user:secret@example.com/",
        "https://example.com:8080/",
    ]:
        with pytest.raises(ValueError):
            resolve_public(url)


def test_fake_dns_never_allows_private_doh_answer(monkeypatch):
    import lead_app.fetch as fetch

    fetch.public_addresses.cache_clear()
    monkeypatch.setattr(
        fetch.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("198.18.0.20", 443))],
    )

    class Resolver:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, *a, **k):
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://1.1.1.1/dns-query"),
                json={"Status": 0, "Answer": [{"type": 1, "data": "127.0.0.1"}]},
            )

    monkeypatch.setattr(fetch.httpx, "Client", Resolver)
    with pytest.raises(ValueError):
        fetch.resolve_public("https://public.example/")
    fetch.public_addresses.cache_clear()


@pytest.mark.asyncio
async def test_structured_chat_applies_once(agent, monkeypatch):
    import lead_app.activities as activities
    from lead_app.model import Decision

    calls = []

    async def stub(*args, **kwargs):
        calls.append(1)
        return Decision(
            reply="希望主要吸引哪类用户？",
            intent="business",
            business_name="隔离验证产品",
            description="用于测试的业务描述",
            audience="小团队",
            stage="audience",
        )

    monkeypatch.setattr(activities, "run_model", stub)
    message = domain.submit_message(agent, "测试业务描述", uid())
    with tx(agent) as c:
        job = one(
            c, "SELECT id FROM lead.chat_jobs WHERE message_id=%s", (message["id"],)
        )
    await activities.chat_job({"agent_id": agent, "job_id": str(job["id"])})
    await activities.chat_job({"agent_id": agent, "job_id": str(job["id"])})
    detail = domain.agent_detail(agent)
    assert len(calls) == 1
    assert detail["business"]["description"] == "用于测试的业务描述"
    assert len(detail["messages"]) == 3


@pytest.mark.asyncio
async def test_deepseek_wire_format(monkeypatch, agent):
    import lead_app.model as model
    from pydantic_ai.providers.openai import OpenAIProvider

    captured = []

    def transport(request):
        data = json.loads(request.content)
        captured.append(data)
        return httpx.Response(
            200,
            json={
                "id": "local-test",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "reply": "你希望面向哪类用户？",
                                    "intent": "answer",
                                    "stage": "audience",
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(
            model,
            "model_config",
            lambda: {
                "key": "test-key",
                "model": "deepseek-flash",
                "base_url": "https://api.deepseek.com",
            },
        )
        monkeypatch.setattr(
            model,
            "OpenAIProvider",
            lambda **kwargs: OpenAIProvider(**kwargs, http_client=client),
        )
        result = await model.run_model(
            model.Decision,
            {"messages": [{"role": "user", "text": "你好"}]},
            agent_id=agent,
        )
        assert result.stage == "audience"
        assert captured[0]["model"] == "deepseek-flash"
        assert captured[0]["thinking"] == {"type": "disabled"}
        with tx(agent) as c:
            assert one(
                c, "SELECT reserved_units,spent_units FROM lead.budget_periods"
            ) == {"reserved_units": 0, "spent_units": 20}


def test_analytics_attribution_is_separate_from_clicks(agent):
    cid = uid()
    code = uid().replace("-", "")[:16]
    with tx(agent) as c:
        c.execute(
            "INSERT INTO lead.analytics_connections(id,tenant_id,agent_id,provider,site_host,secret_ref,state) VALUES(%s,%s,%s,'lead_site','https://business.example','hmac','pending')",
            (cid, TENANT, agent),
        )
        c.execute(
            "INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host) VALUES(%s,%s,'direct',%s,'https://business.example/','business.example')",
            (TENANT, agent, code),
        )
    with TestClient(app) as c:
        r = c.get("/r/" + code, follow_redirects=False)
        assert r.status_code == 302 and "lead_ref=" + code in r.headers["location"]
        with tx(agent) as db:
            m = domain.metrics(db)
            assert m["clicks"] == 1 and m["visits"] is None
        sid = uid()
        body = {
            "key": token("analytics:" + cid),
            "session_id": sid,
            "event": "pageview",
            "url": "https://business.example/",
            "ref": code,
        }
        assert (
            c.post(
                "/collect/" + cid,
                json=body,
                headers={"Origin": "https://wrong.example"},
            ).status_code
            == 403
        )
        assert (
            c.post(
                "/collect/" + cid,
                json=body,
                headers={"Origin": "https://business.example"},
            ).status_code
            == 204
        )
        with tx(agent) as db:
            m = domain.metrics(db)
            assert (
                m["visits"] == 1
                and m["qualified_visits"] == 0
                and m["conversions"] is None
            )
            db.execute(
                "UPDATE lead.visit_sessions SET started_at=now()-interval '15 seconds' WHERE source_session_id=%s",
                (sid,),
            )
        body["event"] = "engagement"
        assert (
            c.post(
                "/collect/" + cid,
                json=body,
                headers={"Origin": "https://business.example"},
            ).status_code
            == 204
        )
        # A direct site event is real traffic but cannot be claimed as Agent acquisition.
        body.update(session_id=uid(), event="pageview", ref=None)
        c.post(
            "/collect/" + cid, json=body, headers={"Origin": "https://business.example"}
        )
        with tx(agent) as db:
            m = domain.metrics(db)
            assert m["visits"] == 1 and m["qualified_visits"] == 1
