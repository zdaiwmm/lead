import os

import pytest
from lead_app import domain, hybrid_research
from lead_app.browser import Gateway
from lead_app.research import begin_run, finish_run


def test_hybrid_is_explicitly_enabled_and_requires_isolated_runtime(
    monkeypatch, tmp_path
):
    runtime = tmp_path / "python"
    monkeypatch.setattr(hybrid_research, "RUNTIME", runtime)
    monkeypatch.setattr(
        hybrid_research, "env", lambda: {"LEAD_BROWSER_USE_ENABLED": "1"}
    )
    assert not hybrid_research.available()
    runtime.touch()
    assert hybrid_research.available()
    monkeypatch.setattr(hybrid_research, "env", lambda: {})
    assert not hybrid_research.available()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="real isolated Chromium and Camoufox",
)
async def test_hybrid_uses_empty_cookie_context_and_blocks_external_writes(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    rid = begin_run(aid, "Verify public-only browser-use environment")
    gateway = Gateway()
    controller = None
    received = []
    try:
        actor = await gateway.get(aid)
        await actor.context.add_cookies(
            [
                {
                    "name": "owner_session",
                    "value": "must_not_copy",
                    "domain": "lead-hybrid-fixture.test",
                    "path": "/",
                }
            ]
        )
        controller = hybrid_research.HybridResearchPage(actor, rid)
        await controller.ensure()
        assert controller.cdp_url.startswith("http://127.0.0.1:")
        assert not await controller.context.cookies()

        async def fixture(route):
            received.append(route.request.method)
            await route.fulfill(
                body="<title>Public fixture</title><p>Public facts only</p>",
                content_type="text/html",
            )

        monkeypatch.setattr(actor, "route", fixture)
        await controller.page.goto("https://lead-hybrid-fixture.test/")
        result = await controller.page.evaluate(
            "() => fetch('/write',{method:'POST',body:'x'}).then(()=>false).catch(()=>true)"
        )
        assert result is True
        assert received == ["GET"]
        assert (await controller.observe())["read_only"] is True
    finally:
        if controller:
            await controller.close()
        finish_run(aid, rid, "succeeded")
        await gateway.close(aid)
