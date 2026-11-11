"""Fault injection against isolated PostgreSQL; no live browser or messages."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from lead_app import admin_notifications as notices
from lead_app import admin_operations as operations
from lead_app import (
    content_backlog,
    domain,
    events,
    operating_kernel,
    owned_audience,
    worker,
)
from lead_app.admin_settings import defaults
from lead_app.db import all_rows, emit, j, one, tx, uid
from lead_app.runtime_issues import active, optional
from lead_app.scope import current_tenant
from test_admin_operations import web
from test_admin_operations import workspace as workspace
from test_content_backlog import agent as agent
from test_content_backlog import author as author

pytestmark = pytest.mark.usefixtures("workspace")


def test_repeated_chat_failure_keeps_agent_and_other_lane_running(agent):
    for _ in range(5):
        assert operations.failure(agent, "chat", "MODEL_UNAVAILABLE")["paused"] is False
    assert operations.admission(agent, "chat")["reason"] == "failure_backoff"
    assert operations.admission(agent, "autonomous")["allowed"]
    assert operations.admission(agent, "publish")["allowed"]
    with tx(agent) as c:
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"]
            == "running"
        )
        assert active(c, agent)[0]["scope"] == "runtime:chat"
    operations.success(agent, "chat")
    with tx(agent) as c:
        assert not active(c, agent)


def test_sql_projection_failure_rolls_back_only_its_savepoint(agent):
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.agents SET name='Core state saved' WHERE id=%s", (agent,)
        )

        def broken():
            c.execute(
                "UPDATE lead.agents SET name='Must rollback' WHERE id=%s", (agent,)
            )
            c.execute("SELECT 1/0")

        assert optional(c, agent, "review", broken) is None
        assert one(c, "SELECT name FROM lead.agents")["name"] == "Core state saved"
    with tx(agent) as c:
        assert active(c, agent)[0]["code"] == "DivisionByZero"


@pytest.mark.asyncio
async def test_failed_plan_and_inbox_do_not_block_content_or_rollback_finish(
    agent, monkeypatch
):
    from lead_app import operating_plans, telegram_inbox

    def choices(*args):
        return (
            [operating_kernel.Candidate("content", "content", 70, "准备文案")],
            {},
            {"review_id": None, "scheduling": {"budget": {}, "deferred": []}},
        )

    def broken_plan(c, *args):
        c.execute("SELECT 1/0")

    async def broken_inbox(*args):
        raise ConnectionError("fixture inbox")

    async def ready(*args, **kwargs):
        return {"state": "ready", "task_id": "local-draft"}

    monkeypatch.setattr(operating_kernel, "candidates", choices)
    monkeypatch.setattr(operating_plans, "sync", broken_plan)
    monkeypatch.setattr(telegram_inbox, "cycle", broken_inbox)
    monkeypatch.setattr(content_backlog, "prepare_one", ready)
    cycle_id = uid()
    result = await operating_kernel.cycle({"agent_id": agent, "cycle_id": cycle_id})
    assert result["state"] == "ready"
    with tx(agent) as c:
        decision = one(
            c,
            "SELECT state,result FROM lead.tasks WHERE workflow_id=%s",
            ("operating-decision:" + cycle_id,),
        )
        assert decision["state"] == "succeeded"
        assert decision["result"]["cycle_result"]["state"] == "ready"
        assert {x["scope"] for x in active(c, agent)} == {"plans", "inbox"}
    assert (await operating_kernel.cycle({"agent_id": agent, "cycle_id": cycle_id}))[
        "duplicate"
    ]


def test_notification_muting_is_not_recovery_and_quiet_hours_do_not_hide_fault(
    agent, monkeypatch
):
    settings = defaults("notifications")
    settings["enabled"] = False
    settings["quiet_hours"].update(
        enabled=True, start="00:00", end="23:59", weekdays=list(range(7))
    )
    now = datetime.now(timezone.utc)
    with tx() as c:
        notices.condition(
            c,
            "fixture",
            "login_expired",
            True,
            aid=agent,
            title="Login",
            message="Login required",
            settings=settings,
            now=now,
        )
        n = one(
            c, "SELECT * FROM lead.admin_notifications WHERE condition_key='fixture'"
        )
        assert n["muted"] and not n["resolved_at"] and n["available_at"] == now
        notices.condition(
            c,
            "fixture",
            "login_expired",
            True,
            aid=agent,
            title="Changed",
            message="Current detail",
            severity="error",
            settings=settings,
            now=now,
        )
        n = one(
            c, "SELECT * FROM lead.admin_notifications WHERE condition_key='fixture'"
        )
        assert (
            n["title"] == "Changed" and n["severity"] == "error" and n["episode"] == 1
        )
        assert one(
            c,
            "SELECT active FROM lead.admin_notification_conditions WHERE condition_key='fixture'",
        )["active"]
        notices.condition(
            c, "fixture", "login_expired", False, aid=agent, settings=settings, now=now
        )
        assert one(
            c,
            "SELECT resolved_at FROM lead.admin_notifications WHERE condition_key='fixture'",
        )["resolved_at"]
    monkeypatch.setattr(
        notices,
        "collect",
        lambda: (_ for _ in ()).throw(AssertionError("GET must not collect")),
    )
    assert web().get("/api/admin/notifications").status_code == 200


@pytest.mark.asyncio
async def test_poison_event_is_deferred_without_holding_later_tasks(agent):
    with tx(agent) as c:
        emit(c, agent, "chat.enqueue", {})  # Persisted malformed event.
        emit(c, agent, "supervisor.wake")
    calls = []

    async def start(*args, **kwargs):
        calls.append(kwargs["id"])

    await worker.deliver_workspace(
        SimpleNamespace(start_workflow=start), current_tenant(), False, False
    )
    assert calls == ["supervisor:" + agent]
    with tx(agent) as c:
        bad = one(c, "SELECT * FROM lead.outbox_events WHERE event_type='chat.enqueue'")
        assert bad["published_at"] is None and bad["delivery_error"] == "KeyError"
        assert (
            bad["available_at"] > datetime.now(timezone.utc)
            and bad["lease_token"] is None
        )
        assert one(
            c,
            "SELECT published_at FROM lead.outbox_events WHERE event_type='supervisor.wake'",
        )["published_at"]


def test_audience_failure_preserves_last_success_and_retries_soon(agent):
    target = uid()
    with tx(agent) as c:
        account = one(c, "SELECT id FROM lead.social_accounts")
        c.execute(
            "INSERT INTO lead.account_targets(id,tenant_id,agent_id,account_id,target_type,external_target_id,display_name,verified_permissions,state) VALUES(%s,lead.ctx_tenant(),%s,%s,'channel','fixture','Fixture','{}','active')",
            (target, agent, account["id"]),
        )
        owned_audience.save(c, target, 12)
        old = one(
            c,
            "SELECT verified_permissions->'owned_audience' value FROM lead.account_targets",
        )["value"]
        owned_audience.save(c, target, None)
        value = one(
            c,
            "SELECT verified_permissions->'owned_audience' value FROM lead.account_targets",
        )["value"]
        assert value["count"] == 12 and value["checked_at"] == old["checked_at"]
        assert owned_audience.status(value, datetime.now(timezone.utc)) == "eligible"
        assert (
            owned_audience.status(
                value, datetime.now(timezone.utc) + timedelta(minutes=2)
            )
            == "due"
        )


@pytest.mark.asyncio
async def test_content_can_be_created_without_a_connected_account(agent, author):
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.social_accounts SET state='pending',external_account_id=NULL"
        )
    assert (await content_backlog.prepare_one(agent))["state"] == "ready"
    with tx(agent) as c:
        assert not all_rows(c, "SELECT id FROM lead.actions")


def test_account_rotation_keeps_identity_history_and_recycles_only_unsent_copy(agent):
    from lead_app.channels import store_account

    with tx(agent) as c:
        old = one(c, "SELECT * FROM lead.social_accounts")
        before = one(c, "SELECT control_version,config_revision FROM lead.agents")
        target = one(
            c,
            "INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,display_name,verified_permissions,state) VALUES(lead.ctx_tenant(),%s,%s,'channel','fixture','Fixture','{}','active') RETURNING id",
            (agent, old["id"]),
        )["id"]
        grant = one(
            c,
            "INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,granted_by) VALUES(lead.ctx_tenant(),%s,%s,1,'[\"publish\"]','fixture') RETURNING id",
            (agent, target),
        )["id"]
        pairs = []
        for uncertain in (False, True):
            draft, task = uid(), uid()
            c.execute(
                "INSERT INTO lead.tasks(id,tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,lead.ctx_tenant(),%s,'content.prepare','succeeded',%s,'{}',%s,now())",
                (
                    draft,
                    agent,
                    uid(),
                    j(
                        {
                            "status": "scheduled",
                            "publication_task_id": task,
                            "body": "Preserve copy",
                        }
                    ),
                ),
            )
            c.execute(
                "INSERT INTO lead.tasks(id,tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,lead.ctx_tenant(),%s,'telegram.publish','queued',%s,%s,now())",
                (
                    task,
                    agent,
                    uid(),
                    j({"target_id": str(target), "prepared_content_id": draft}),
                ),
            )
            if uncertain:
                c.execute(
                    "INSERT INTO lead.actions(tenant_id,agent_id,task_id,target_id,grant_id,operation_key,type,intent,expected_control_version,expected_config_revision,state) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,'publish','{}',1,1,'unknown')",
                    (agent, task, target, grant, uid()),
                )
            pairs.append((draft, task, uncertain))
    new_id = store_account(
        agent,
        "telegram",
        {"external_account_id": uid(), "display_name": "New account"},
        prompt_target=False,
    )
    with tx(agent) as c:
        archived = one(
            c, "SELECT * FROM lead.social_accounts WHERE id=%s", (old["id"],)
        )
        assert (
            archived["state"] == "disconnected"
            and archived["external_account_id"] == old["external_account_id"]
        )
        assert new_id != str(old["id"])
        assert (
            one(c, "SELECT control_version,config_revision FROM lead.agents") == before
        )
        for draft, task, uncertain in pairs:
            assert one(
                c,
                "SELECT result->>'status' value FROM lead.tasks WHERE id=%s",
                (draft,),
            )["value"] == ("scheduled" if uncertain else "ready")
            assert one(c, "SELECT state FROM lead.tasks WHERE id=%s", (task,))[
                "state"
            ] == ("queued" if uncertain else "canceled")
        assert one(c, "SELECT state FROM lead.actions")["state"] == "unknown"


def test_revision_changes_for_fault_detail_without_changing_task_state(agent):
    with tx(agent) as c:
        tid = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(lead.ctx_tenant(),%s,'fixture','failed',%s,'{}','{}',now()) RETURNING id",
            (agent, uid()),
        )["id"]
    before = events.revision()
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.tasks SET result=%s WHERE id=%s",
            (j({"reason": "New failure detail"}), tid),
        )
    assert events.revision() != before


def test_metrics_failure_leaves_agent_and_chat_readable(agent, monkeypatch):
    def broken(c):
        c.execute("SELECT 1/0")

    monkeypatch.setattr(domain, "metrics", broken)
    assert any(str(a["id"]) == agent for a in domain.list_agents())
    detail = domain.agent_detail(agent)
    assert detail["metrics"]["available"] is False and detail["runtime_issues"]
    assert domain.agent_detail(agent, chat=True)["business"]


def test_external_notification_failure_keeps_in_app_fact_and_unmute_reuses_episode(
    agent, monkeypatch
):
    from lead_app import admin_delivery

    settings = defaults("notifications")
    with tx() as c:
        notices.condition(
            c,
            "external-failure",
            "login_expired",
            True,
            aid=agent,
            message="Needs login",
            settings=settings,
        )

    def broken(*args):
        raise ValueError("unavailable destination configuration")

    monkeypatch.setattr(admin_delivery, "enqueue", broken)
    admin_delivery.enqueue_pending()
    with tx() as c:
        row = one(
            c,
            "SELECT * FROM lead.admin_notifications WHERE condition_key='external-failure'",
        )
        assert row["external_pending"] and row["resolved_at"] is None
        settings["enabled"] = False
        notices.condition(
            c,
            "external-failure",
            "login_expired",
            True,
            aid=agent,
            message="Needs login",
            settings=settings,
        )
        settings["enabled"] = True
        notices.condition(
            c,
            "external-failure",
            "login_expired",
            True,
            aid=agent,
            message="Needs login",
            settings=settings,
        )
        row = one(
            c,
            "SELECT * FROM lead.admin_notifications WHERE condition_key='external-failure'",
        )
        assert not row["muted"] and row["episode"] == 1 and row["external_pending"]


def test_light_status_does_not_call_metrics_or_notifications(agent, monkeypatch):
    def forbidden(*args):
        raise AssertionError("derived service must not gate status")

    monkeypatch.setattr(domain, "metrics", forbidden)
    monkeypatch.setattr(notices, "collect", forbidden)
    response = web().get(f"/api/agents/{agent}/status")
    assert response.status_code == 200 and response.json()["desired_state"] == "running"


def test_optional_measurement_uses_public_destination_but_explicit_tracking_still_waits(
    monkeypatch,
):
    from lead_app import outbound_links, promotion

    monkeypatch.setattr(
        outbound_links, "configured_problem", lambda: "public_shortlink_required"
    )
    marker = promotion.new_marker("telegram")
    text, link = promotion.promotion_body(
        "Verified product note", "https://product.example/start", marker
    )
    assert (
        link.startswith("https://product.example/start?")
        and marker in link
        and link in text
    )
    with pytest.raises(ValueError, match="public_shortlink_required"):
        promotion.promotion_body(
            "Verified note",
            "https://product.example/start",
            marker,
            require_tracking=True,
        )


def test_replacing_account_archives_its_old_browser_challenge(agent):
    from lead_app import browser_challenges, channels

    browser_challenges.block(SimpleNamespace(aid=agent, session=uid()), "telegram")
    channels.store_account(
        agent,
        "telegram",
        {"external_account_id": uid(), "display_name": "Replacement"},
        prompt_target=False,
    )
    with tx(agent) as c:
        assert browser_challenges.pending(c) is None
        assert not any(i["scope"].startswith("browser:") for i in active(c, agent))
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"]
            == "running"
        )


@pytest.mark.asyncio
async def test_slow_dispatch_does_not_hold_an_independent_task(agent):
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    with tx(agent) as c:
        emit(c, agent, "chat.enqueue", {"job_id": uid()})
        emit(c, agent, "supervisor.wake")

    async def start(*args, **kwargs):
        if kwargs["id"].startswith("chat:"):
            await release.wait()
        else:
            started.set()

    dispatch = asyncio.create_task(
        worker.deliver_workspace(
            SimpleNamespace(start_workflow=start), current_tenant(), False, False
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not release.is_set()
    finally:
        release.set()
        await dispatch


def test_connecting_replacement_after_explicit_disconnect_enables_new_account(agent):
    from lead_app import channels

    with tx(agent) as c:
        old = one(c, "SELECT * FROM lead.social_accounts")
    channels.disconnect_account(agent, str(old["id"]), old["verified_at"])
    new = channels.store_account(
        agent,
        "telegram",
        {"external_account_id": uid(), "display_name": "Replacement"},
        prompt_target=False,
    )
    with tx(agent) as c:
        row = one(
            c,
            "SELECT state,automation_enabled FROM lead.social_accounts WHERE id=%s",
            (new,),
        )
        assert row == {"state": "connected", "automation_enabled": True}


def test_disconnecting_one_account_keeps_other_profile_work_and_challenge(agent, monkeypatch):
    from lead_app import account_profiles, browser_challenges, channels

    monkeypatch.setenv('LEAD_CHANNEL_ATOM_AGENT_IDS', agent)
    profile = account_profiles.for_platform(agent, 'reddit', create=True, new_binding=True)
    with tx(agent) as c:
        old = one(c, "SELECT * FROM lead.social_accounts WHERE platform='telegram'")
        sibling = one(c, "UPDATE lead.social_accounts SET state='connected',automation_enabled=true,external_account_id=%s WHERE profile_id=%s RETURNING *", (uid(), profile['id']))
        task = one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at)
            VALUES(lead.ctx_tenant(),%s,'reddit.publish','planned',%s,%s,now()) RETURNING id""",
            (agent, uid(), j({'platform':'reddit','account_id':str(sibling['id'])})))
        before = one(c, 'SELECT control_version,authorization_version,config_revision FROM lead.agents')
    browser_challenges.block(SimpleNamespace(aid=agent, session=uid(), profile=profile), 'reddit')
    channels.disconnect_account(agent, str(old['id']), old['verified_at'])
    with tx(agent) as c:
        assert browser_challenges.pending(c, profile['id'])['account_id'] == str(sibling['id'])
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (task['id'],))['state']=='planned'
        assert one(c, 'SELECT control_version,authorization_version,config_revision FROM lead.agents') == before
        assert any(i['scope']=='browser:'+str(profile['id']) for i in active(c, agent))


def test_channel_leases_and_chat_have_independent_capacity(agent):
    assert operations.admission(agent, 'channel_delivery', lane='channel:reddit')['allowed']
    assert operations.admission(agent, 'channel_delivery', lane='channel:facebook')['allowed']
    assert operations.admission(agent, 'chat')['allowed']
    assert operations.admission(agent, 'channel_delivery', lane='channel:reddit')['reason']=='concurrency_limit'


@pytest.mark.asyncio
async def test_release_fence_keeps_business_consumers_waiting_and_can_stop(monkeypatch, tmp_path):
    import asyncio
    monkeypatch.setattr(worker, 'STATE', tmp_path)
    monkeypatch.setattr(worker, 'release_fenced', lambda: True)
    stop = asyncio.Event()
    waiting = asyncio.create_task(worker.await_release_window(stop))
    await asyncio.sleep(.05)
    assert not waiting.done()
    assert (tmp_path / 'worker-heartbeat').exists()
    stop.set()
    assert not await waiting
    stop.clear()
    monkeypatch.setattr(worker, 'release_fenced', lambda: False)
    assert await worker.await_release_window(stop)
