"""Actual Camoufox + PostgreSQL + HTTP fixture. No social account or model key.
Run explicitly with LEAD_BROWSER_TESTS=1 and the local API at :8088.
"""

import asyncio
import os
import time

import pytest
from lead_app import domain
from lead_app.activities import prepare_action
from lead_app.browser import Gateway
from lead_app.db import one, tx

pytestmark = pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="opt-in real browser integration",
)


@pytest.mark.asyncio
async def test_concurrent_profiles_restart_and_uncertain_submit(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *args, **kwargs: None)
    ids = [domain.create_agent() for _ in range(3)]
    g = Gateway()
    try:
        for aid in ids:
            domain.connect_lab(aid)
        tasks = [
            domain.queue_lab_publish(aid, "独立环境 " + aid, "parallel-" + aid)
            for aid in ids
        ]
        actions = [prepare_action(aid, str(t["id"])) for aid, t in zip(ids, tasks)]
        actors = await asyncio.gather(*(g.get(aid) for aid in ids))
        assert actors[0].context is not actors[1].context
        assert len(g.actors) == 3
        await asyncio.gather(
            *(a.page.goto("http://127.0.0.1:8088/lab/" + a.aid + "/") for a in actors)
        )

        async def presented_identity(page):
            import json

            origin = page.url.split("/lab/")[0]
            aid = page.url.split("/lab/")[1].split("/")[0]
            await page.goto(origin + "/lab/" + aid + "/identity/")
            await page.locator("#result[data-ready=true]").wait_for()
            return json.loads(await page.locator("#result").inner_text())

        presented = await asyncio.gather(*(presented_identity(a.page) for a in actors))
        assert presented[0] != presented[1]
        # Both environments are active at once and have different stable identities.
        barrier = asyncio.Barrier(3)
        intervals = []

        async def operate(actor, action):
            await barrier.wait()
            start = time.monotonic()
            await asyncio.sleep(0.5)
            result = await actor.publish_lab(str(action["id"]))
            intervals.append((start, time.monotonic()))
            return result

        results = await asyncio.gather(
            *(operate(a, act) for a, act in zip(actors, actions))
        )
        assert all(r["state"] == "confirmed" for r in results), results
        assert max(s for s, e in intervals) < min(e for s, e in intervals)
        for aid in ids:
            with tx(aid) as c:
                assert one(c, "SELECT count(*) AS n FROM lead.lab_posts")["n"] == 1
        # A profile cookie/localStorage survives a real browser process restart.
        actor = actors[0]
        await actor.page.evaluate(
            "localStorage.setItem('lead-isolation-test','persistent')"
        )
        assert (
            await actors[1].page.evaluate("localStorage.getItem('lead-isolation-test')")
            is None
        )
        fingerprint = (
            __import__("lead_app.config", fromlist=["STATE"]).STATE
            / "profiles"
            / ids[0]
            / "identity.json"
        ).read_bytes()
        await g.close(ids[0])
        actor = await g.get(ids[0])
        await actor.page.goto("http://127.0.0.1:8088/lab/" + ids[0] + "/")
        assert (
            await actor.page.evaluate("localStorage.getItem('lead-isolation-test')")
            == "persistent"
        )
        assert (
            fingerprint
            == (
                __import__("lead_app.config", fromlist=["STATE"]).STATE
                / "profiles"
                / ids[0]
                / "identity.json"
            ).read_bytes()
        )
        assert presented[0] == await presented_identity(actor.page)
        # Lose acknowledgement after the real submit. Reconcile, never resubmit.
        task = domain.queue_lab_publish(ids[0], "确认丢失验证", "lost-ack")
        action = prepare_action(ids[0], str(task["id"]))
        first = await actor.publish_lab(str(action["id"]), fault=True)
        assert first["state"] == "unknown"
        second = await actor.publish_lab(str(action["id"]))
        assert second["state"] == "confirmed" and second["reconciled"]
        with tx(ids[0]) as c:
            assert (
                one(
                    c,
                    "SELECT count(*) AS n FROM lead.lab_posts WHERE operation_key='lost-ack'",
                )["n"]
                == 1
            )
            assert (
                one(
                    c,
                    "SELECT count(*) AS n FROM lead.action_attempts WHERE action_id=%s",
                    (action["id"],),
                )["n"]
                == 1
            )
        # Pause fences an already prepared browser action.
        task = domain.queue_lab_publish(ids[0], "暂停后不应发布", "fenced")
        action = prepare_action(ids[0], str(task["id"]))
        domain.set_control(ids[0], "pause")
        with pytest.raises(ValueError):
            await actor.publish_lab(str(action["id"]))
        with tx(ids[0]) as c:
            assert not one(
                c, "SELECT id FROM lead.lab_posts WHERE operation_key='fenced'"
            )
        extra = domain.create_agent()
        try:
            fourth = await asyncio.wait_for(g.get(extra), timeout=20)
            assert fourth.aid == extra and len(g.actors) == 3
            assert len(set(ids) & set(g.actors)) == 2
        finally:
            await g.close(extra)
            domain.set_control(extra, "archive")
    finally:
        for aid in ids:
            await g.close(aid)
            domain.set_control(aid, "archive")
