"""Concurrency probes exercise the same activities and durable tasks as the worker."""

import threading
from concurrent.futures import ThreadPoolExecutor

from lead_app import atomic_dispatch, domain
from lead_app.db import all_rows, one, tx, uid
from lead_app.scope import current_tenant


def test_dynamic_membership_and_yield(monkeypatch):
    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_CHANNEL_ATOM_AGENT_IDS", aid)
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            """INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description)
            VALUES(%s,%s,1,'Pilot product')""",
            (current_tenant(), aid),
        )
        profile = one(c, "SELECT id FROM lead.browser_profiles")["id"]
        for platform in ("reddit", "facebook", "instagram"):
            c.execute(
                """INSERT INTO lead.social_accounts
                (tenant_id,agent_id,profile_id,platform,external_account_id,state,automation_enabled)
                VALUES(%s,%s,%s,%s,%s,'connected',false)""",
                (current_tenant(), aid, profile, platform, uid()),
            )

    def task(platform):
        with tx(aid) as c:
            return one(
                c,
                "SELECT * FROM lead.tasks WHERE type='dispatch.channel' AND input->>'platform'=%s",
                (platform,),
            )

    def run(t):
        atomic_dispatch.execute_connected(aid, t["id"], t["version"])

    started = threading.Event()
    release = threading.Event()
    original = atomic_dispatch.draft_pack

    def delayed(agent, business, channel, version):
        if channel["platform"] == "reddit":
            started.set()
            assert release.wait(10)
        return original(agent, business, channel, version)

    monkeypatch.setattr(atomic_dispatch, "draft_pack", delayed)
    for platform in ("reddit", "facebook"):
        domain.channel_control(aid, platform, True)
    assert not task("instagram")
    with ThreadPoolExecutor(2) as pool:
        reddit = pool.submit(run, task("reddit"))
        try:
            assert started.wait(5)
            pool.submit(run, task("facebook")).result(timeout=5)
            assert task("facebook")["state"] == "succeeded"
            old = task("reddit")
            domain.channel_control(aid, "reddit", False)
            domain.channel_control(aid, "instagram", True)
            pool.submit(run, task("instagram")).result(timeout=5)
            assert task("instagram")["state"] == "succeeded"
            domain.channel_control(aid, "reddit", True)
            assert task("reddit")["version"] > old["version"]
        finally:
            release.set()
        reddit.result(timeout=5)
    # Original generation completed late. It cannot commit against the new one.
    assert task("reddit")["state"] == "queued"
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.social_accounts SET state='needs_auth' WHERE platform='reddit'"
        )
    run(task("reddit"))
    blocked = task("reddit")
    assert (
        blocked["state"] == "waiting_user"
        and blocked["result"]["reason"] == "channel_connection_required"
    )
    with tx(aid) as c:
        atomic_dispatch.refresh_connected(c, aid)
        assert (
            one(c, "SELECT version FROM lead.tasks WHERE id=%s", (blocked["id"],))[
                "version"
            ]
            == blocked["version"]
        )
        c.execute(
            "UPDATE lead.social_accounts SET state='connected' WHERE platform='reddit'"
        )
    domain.channel_control(aid, "reddit", True)
    run(task("reddit"))
    with tx(aid) as c:
        products = all_rows(
            c,
            "SELECT input->>'platform' AS platform FROM lead.tasks WHERE type='channel.prepare'",
        )
        assert sorted(p["platform"] for p in products) == [
            "facebook",
            "instagram",
            "reddit",
        ]


async def test_real_temporal_connected_channels(monkeypatch):
    """Run only against a disposable Temporal server supplied by the test operator."""
    import asyncio
    import os

    import pytest
    from lead_app.worker import dispatch_channel_event
    from lead_app.workflows import ChannelPreparationWorkflow
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    address = os.environ.get("LEAD_TEST_CHANNEL_TEMPORAL")
    if not address:
        pytest.skip("requires isolated Temporal server")
    client = await Client.connect(address)
    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_CHANNEL_ATOM_AGENT_IDS", aid)
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            """INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description)
            VALUES(%s,%s,1,'Fixture')""",
            (current_tenant(), aid),
        )
        for platform in ("reddit", "facebook", "instagram"):
            c.execute(
                """INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,
                external_account_id,state,automation_enabled)
                SELECT tenant_id,agent_id,id,%s,%s,'connected',false FROM lead.browser_profiles""",
                (platform, uid()),
            )

    def delivery(platform):
        with tx(aid) as c:
            return one(
                c,
                """SELECT e.payload FROM lead.outbox_events e JOIN lead.tasks t
                ON t.id=(e.payload->>'task_id')::uuid WHERE e.event_type='dispatch.channel'
                AND t.input->>'platform'=%s ORDER BY e.occurred_at DESC LIMIT 1""",
                (platform,),
            )["payload"]

    started = threading.Event()
    release = threading.Event()
    original = atomic_dispatch.draft_pack

    def slow(agent, business, channel, version):
        if channel["platform"] == "reddit":
            started.set()
            assert release.wait(20)
        return original(agent, business, channel, version)

    monkeypatch.setattr(atomic_dispatch, "draft_pack", slow)
    async with Worker(
        client,
        task_queue="lead-offline",
        workflows=[ChannelPreparationWorkflow],
        activities=[
            atomic_dispatch.prepare_channel_atom,
            atomic_dispatch.close_channel_atom,
        ],
        max_concurrent_activities=2,
    ):
        domain.channel_control(aid, "reddit", True)
        r = await dispatch_channel_event(client, aid, delivery("reddit"))
        try:
            assert await asyncio.to_thread(started.wait, 10)
            domain.channel_control(aid, "facebook", True)
            f = await dispatch_channel_event(client, aid, delivery("facebook"))
            await asyncio.wait_for(f.result(), 10)
            assert not release.is_set()
            # Actual account state changes while Reddit is between its read and commit.
            with tx(aid) as c:
                c.execute(
                    "UPDATE lead.social_accounts SET state='needs_auth' WHERE platform='reddit'"
                )
        finally:
            release.set()
        await asyncio.wait_for(r.result(), 10)
        with tx(aid) as c:
            assert (
                one(
                    c,
                    "SELECT state FROM lead.tasks WHERE type='dispatch.channel' AND input->>'platform'='reddit'",
                )["state"]
                == "waiting_user"
            )
        domain.channel_control(aid, "facebook", False)
        domain.channel_control(aid, "instagram", True)
        third = await dispatch_channel_event(client, aid, delivery("instagram"))
        await asyncio.wait_for(third.result(), 10)
        from lead_app.channels import store_account

        with tx(aid) as c:
            account = one(
                c,
                "SELECT external_account_id FROM lead.social_accounts WHERE platform='reddit'",
            )

        def no_new_draft(*args):
            pytest.fail("recovery discarded the completed draft checkpoint")

        monkeypatch.setattr(atomic_dispatch, "draft_pack", no_new_draft)
        store_account(
            aid, "reddit", {**account, "display_name": "Reddit"}, prompt_target=False
        )
        # Contend on a real PostgreSQL lock. The workflow must reach a durable
        # timer (no live activity/worker slot) before the lock is released.
        from temporalio.api.enums.v1 import EventType

        payload = delivery("reddit")
        with tx(aid) as c:
            one(
                c,
                "SELECT id FROM lead.tasks WHERE id=%s FOR UPDATE",
                (payload["task_id"],),
            )
            recovered = await dispatch_channel_event(client, aid, payload)
            async with asyncio.timeout(8):
                while True:
                    history = await recovered.fetch_history()
                    if any(
                        e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
                        for e in history.events
                    ):
                        break
                    await asyncio.sleep(0.1)
        await asyncio.wait_for(recovered.result(), 10)
        assert r.id != recovered.id
        await dispatch_channel_event(client, aid, delivery("reddit"))
        await Replayer(workflows=[ChannelPreparationWorkflow]).replay_workflow(
            await recovered.fetch_history()
        )
        with tx(aid) as c:
            rows = all_rows(
                c,
                "SELECT input->>'platform' AS platform FROM lead.tasks WHERE type='channel.prepare'",
            )
            assert sorted(row["platform"] for row in rows) == [
                "facebook",
                "instagram",
                "reddit",
            ]
