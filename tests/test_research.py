import asyncio
import json
import os
from uuid import uuid4

import pytest
from lead_app import domain
from lead_app.browser import Gateway
from lead_app.browser_tools import website_for_message
from lead_app.db import one, tx
from lead_app.research import ResearchPage, ResearchStopped, begin_run, finish_run


def test_uppercase_and_followup_urls():
    assert website_for_message("Https://seka.one", [], None) == "https://seka.one"
    assert website_for_message("seka.one", [], None) == "https://seka.one"
    assert (
        website_for_message(
            "那你尝试抓一下", [{"role": "user", "text": "Https://seka.one"}], None
        )
        == "https://seka.one"
    )
    assert (
        website_for_message("看看这个官网", [], {"product_url": "https://seka.one"})
        == "https://seka.one"
    )
    assert (
        website_for_message("请记住用英文", [], {"product_url": "https://seka.one"})
        is None
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="real public browser integration",
)
async def test_three_public_browser_runs_are_concurrent_scoped_and_fenced(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    ids = [domain.create_agent() for _ in range(3)]
    gateway = Gateway()
    runs = [begin_run(aid, "读取真实公开网页") for aid in ids]
    try:
        actors = await asyncio.gather(*(gateway.get(aid) for aid in ids))
        controllers = [ResearchPage(actor, rid) for actor, rid in zip(actors, runs)]
        commands = [str(uuid4()) for _ in ids]
        results = await asyncio.gather(
            *(
                controller.command("open", {"url": "https://seka.one"}, cmd)
                for controller, cmd in zip(controllers, commands)
            )
        )
        intervals = []
        for aid, rid, result in zip(ids, runs, results):
            assert "Seka" in result["title"] and len(result["text"]) > 100
            with tx(aid) as c:
                step = one(
                    c, "SELECT * FROM lead.browser_steps WHERE run_id=%s", (rid,)
                )
                intervals.append((step["started_at"], step["ended_at"]))
                assert (
                    one(c, "SELECT count(*) AS n FROM lead.knowledge_documents")["n"]
                    == 1
                )
                assert one(c, "SELECT count(*) AS n FROM lead.browser_runs")["n"] == 1
        assert max(s for s, e in intervals) < min(e for s, e in intervals)
        from lead_app.config import STATE

        (STATE / "concurrency-verification.json").write_text(
            json.dumps(
                {
                    "agent_ids": ids,
                    "run_ids": runs,
                    "public_url": "https://seka.one",
                    "intervals": [[s.isoformat(), e.isoformat()] for s, e in intervals],
                    "all_three_overlap": True,
                },
                indent=2,
            )
        )
        # Same command returns actual recorded output and does not browse twice.
        duplicate = await controllers[0].command(
            "open", {"url": "https://seka.one"}, commands[0]
        )
        assert duplicate["evidence_id"] == results[0]["evidence_id"]
        with tx(ids[0]) as c:
            assert one(c, "SELECT count(*) AS n FROM lead.browser_steps")["n"] == 1
        domain.set_control(ids[0], "pause")
        with pytest.raises(ResearchStopped):
            await controllers[0].command(
                "open", {"url": "https://seka.one"}, str(uuid4())
            )
        # One stopped Agent does not affect the others.
        assert (
            "Seka" in (await controllers[1].command("read", {}, str(uuid4())))["title"]
        )
        # A genuine browser-process replacement restores the last durable public
        # location. Old element references are not reused across the restart.
        await gateway.close(ids[2])
        recovered = ResearchPage(await gateway.get(ids[2]), runs[2])
        restored = await recovered.command("read", {}, str(uuid4()))
        assert "Seka" in restored["title"] and restored["url"].startswith(
            "https://seka.one"
        )
        assert restored["elements"]
        for aid, rid in zip(ids, runs):
            finish_run(aid, rid)
    finally:
        for aid in ids:
            await gateway.close(aid)
            domain.set_control(aid, "archive")
