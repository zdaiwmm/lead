"""Exercise the owner login flow with the home node's Telegram-only route."""

import json
import os
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from lead_app import account_profiles, api, browser, domain
from lead_app.db import one, tx
from lead_app.scope import current_tenant
from starlette.requests import Request
from test_adapters import FixtureSite


def test_existing_shared_account_requires_review(monkeypatch):
    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_SPLIT_REDDIT_LOGIN_AGENT_IDS", aid)
    with tx(aid) as c:
        base = one(c, "SELECT id FROM lead.browser_profiles WHERE slot='default'")
        c.execute(
            """INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state)
            VALUES(%s,%s,%s,'reddit','pending')""",
            (current_tenant(), aid, base["id"]),
        )
    with pytest.raises(ValueError, match="原浏览器环境"):
        account_profiles.for_platform(aid, "reddit", create=True, isolated_login=True)
    with tx(aid) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.browser_profiles")["n"] == 1


@pytest.mark.skipif(
    os.getenv("LEAD_BROWSER_TESTS") != "1", reason="requires native Camoufox"
)
async def test_reddit_retries_and_completes_on_its_own_browser(monkeypatch):
    aid = domain.create_agent()
    other = domain.create_agent()
    monkeypatch.setenv("LEAD_SPLIT_REDDIT_LOGIN_AGENT_IDS", aid)
    site = FixtureSite("reddit")
    actors = browser.Gateway()
    monkeypatch.setattr(browser, "gateway", actors)
    launch = browser.Actor.launch

    async def launch_fixture(actor):
        await launch(actor)
        platform = (
            FixtureSite("telegram") if actor.profile["slot"] == "default" else site
        )
        await actor.context.route("https://**/*", platform.route)

    monkeypatch.setattr(browser.Actor, "launch", launch_fixture)
    ports = []
    ordinary = httpx.ASGITransport(app=browser.app)
    unavailable = True

    async def send(request):
        ports.append((request.url.path, request.url.port))
        if request.url.port == 8089:
            if (
                request.url.path.endswith("/open")
                and json.loads(request.content)["platform"] != "telegram"
            ):
                return httpx.Response(403)
            return await ordinary.handle_async_request(request)
        assert request.url.port == 18091
        if unavailable:
            raise httpx.ConnectError("ordinary browser offline")
        return await ordinary.handle_async_request(request)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        api.httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(send), **kw),
    )
    connect = api.Connection(platform="reddit")
    req = Request({"type": "http", "headers": []})
    try:
        tg = await api.connect(aid, api.Connection(platform="telegram"))
        telegram_actor = actors.actors[aid]
        await telegram_actor.context.add_cookies(
            [
                {
                    "url": "https://site.example",
                    "name": "telegram_fixture",
                    "value": "private",
                }
            ]
        )
        assert ports.pop() == (f"/agents/{aid}/open", 8089)
        with tx(aid) as c:
            before = one(
                c,
                "SELECT desired_state,control_version FROM lead.agents WHERE id=%s",
                (aid,),
            )
            assert (
                api.login_browser_port(
                    c, aid, f"/agents/{aid}/open", {"platform": "telegram"}
                )
                == 8089
            )
        with pytest.raises(HTTPException) as offline:
            await api.connect(aid, connect)
        assert offline.value.status_code == 503
        with tx(aid) as c:
            pending = one(
                c, "SELECT id FROM lead.social_accounts WHERE platform='reddit'"
            )
            assert one(c, "SELECT count(*) AS n FROM lead.takeover_sessions")["n"] == 1
        unavailable = False
        opened = await api.connect(aid, connect)
        assert (
            opened["account_id"] == pending["id"] and not opened["managed_target_start"]
        )
        takeover = opened["takeover_id"]
        profile = account_profiles.for_platform(aid, "reddit")
        actor = actors.actors[str(profile["id"])]
        assert actor.profile["slot"] == "reddit"
        assert actor.page.url == "https://www.reddit.com/login/"
        assert str(actor.folder).endswith(f"/{aid}/accounts/{profile['id']}")
        assert actor.session != telegram_actor.session
        assert not any(
            cookie["name"] == "telegram_fixture"
            for cookie in await actor.context.cookies()
        )
        assert actor.folder != telegram_actor.folder
        assert (await api.screenshot(UUID(aid), req, "jpeg", takeover)).body.startswith(
            b"\xff\xd8"
        )
        await actor.page.locator("textarea[name=title]").focus()
        await api.browser_input(
            UUID(aid), api.BrowserInput(kind="text", text="fixture", takeover=takeover)
        )
        assert (
            await actor.page.locator("textarea[name=title]").input_value() == "fixture"
        )
        with tx(other) as c:
            assert (
                api.login_browser_port(
                    c, other, f"/agents/{other}/open", {"platform": "reddit"}
                )
                == 8089
            )
        # A takeover from another project cannot select this direct gateway.
        monkeypatch.setenv("LEAD_SPLIT_REDDIT_LOGIN_AGENT_IDS", f"{aid},{other}")
        with pytest.raises(HTTPException):
            await api.screenshot(UUID(other), req, "jpeg", takeover)
        # The owner has finished the website's login flow and left /login/.
        await actor.page.goto("https://www.reddit.com/")
        verified = await api.verify_login(
            UUID(aid), api.Connection(platform="reddit", account_id=pending["id"])
        )
        assert verified["ok"]
        assert [path.rsplit("/", 1)[-1] for path, _ in ports] == [
            "open",
            "open",
            "screenshot",
            "input",
            "inspect",
        ]
        assert {port for _, port in ports} == {18091}
        with pytest.raises(HTTPException) as stale:
            await api.screenshot(UUID(aid), req, "jpeg", takeover)
        assert stale.value.status_code == 409
        with pytest.raises(HTTPException) as ended:
            await api.verify_login(
                UUID(aid), api.Connection(platform="reddit", account_id=pending["id"])
            )
        assert ended.value.status_code == 409
        tg_frame = await api.screenshot(UUID(aid), req, "jpeg", tg["takeover_id"])
        assert tg_frame.body.startswith(b"\xff\xd8") and ports[-1][1] == 8089
        assert any(
            cookie["name"] == "telegram_fixture"
            for cookie in await telegram_actor.context.cookies()
        )
        api.end_takeover(UUID(aid), tg["takeover_id"])
        again = await api.connect(aid, connect)
        api.end_takeover(UUID(aid), again["takeover_id"])
        with tx(aid) as c:
            assert (
                one(
                    c,
                    "SELECT count(*) AS n FROM lead.social_accounts WHERE platform='reddit'",
                )["n"]
                == 1
            )
            assert one(
                c,
                "SELECT state,automation_enabled FROM lead.social_accounts WHERE id=%s",
                (pending["id"],),
            ) == {"state": "connected", "automation_enabled": False}
            assert (
                one(
                    c,
                    "SELECT desired_state,control_version FROM lead.agents WHERE id=%s",
                    (aid,),
                )
                == before
            )
            assert (
                one(
                    c,
                    "SELECT count(*) AS n FROM lead.takeover_sessions WHERE state='active'",
                )["n"]
                == 0
            )
        assert not site.posts
    finally:
        await actors.close(aid)
