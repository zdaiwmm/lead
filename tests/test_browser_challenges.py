"""Verification handoff uses isolated test agents and never real platform writes."""

import asyncio
import inspect
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from lead_app import activities, browser, domain
from lead_app import browser_challenges as challenges
from lead_app.adapters.core import (
    HumanVerificationRequired,
    NeedsAuth,
    check_challenge,
    check_surface,
)
from lead_app.db import all_rows, one, tx, uid


def one_profile(aid):
    with tx(aid) as c:
        return one(c, "SELECT * FROM lead.browser_profiles WHERE slot='default'")


@pytest.fixture
def handoff(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(
        challenges,
        "emit",
        lambda c, aid, kind, payload=None: events.append((kind, payload)),
    )
    aid = domain.create_agent()
    domain.connect_lab(aid)
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute("UPDATE lead.social_accounts SET automation_enabled=true")
    task = domain.queue_lab_publish(aid, "Verification fixture", uid())
    action = activities.prepare_action(aid, str(task["id"]))
    actor = SimpleNamespace(
        aid=aid,
        session=uid(),
        profile=one_profile(aid),
        disconnected=False,
        lock=asyncio.Lock(),
        touch=lambda: None,
    )
    return actor, task, action, events


def mark(handoff):
    actor, _, action, _ = handoff
    result = challenges.block(actor, "lab", str(action["id"]))
    with tx(actor.aid) as c:
        pending = challenges.pending(c)
    return result, pending


@pytest.mark.parametrize("submitted", [False, True])
def test_resume_original_action_and_reconcile_uncertain_submission(handoff, submitted):
    actor, task, action, events = handoff
    if submitted:
        with tx(actor.aid) as c:
            c.execute(
                "UPDATE lead.actions SET state='dispatching' WHERE id=%s",
                (action["id"],),
            )
    _, challenge = mark(handoff)
    challenges.block(actor, "lab", str(action["id"]))
    with tx(actor.aid) as c:
        assert (
            one(c, "SELECT state FROM lead.tasks WHERE id=%s", (task["id"],))["state"]
            == "waiting_user"
        )
        assert (
            one(
                c,
                "SELECT count(*) n FROM lead.audit_events WHERE type='browser.challenge.detected'",
            )["n"]
            == 1
        )
    result = challenges.finish(
        actor, challenge["id"], {"external_account_id": actor.aid}
    )
    assert result["resumed_tasks"] == 1
    assert events[0] == (
        "task.reconcile",
        {"task_id": str(task["id"]), "recovery_id": challenge["id"]},
    )
    with tx(actor.aid) as c:
        assert not challenges.pending(c)
        assert one(c, "SELECT state FROM lead.tasks WHERE id=%s", (task["id"],))[
            "state"
        ] == ("reconciling" if submitted else "queued")
        rows = all_rows(c, "SELECT id,state,operation_key FROM lead.actions")
        assert len(rows) == 1 and rows[0]["id"] == action["id"]
        assert rows[0]["state"] == ("unknown" if submitted else "prepared")


@pytest.mark.parametrize(
    "change", ["pause", "control", "config", "authorization", "disable"]
)
def test_verification_never_overrides_user_controls(handoff, change):
    actor, task, _, events = handoff
    _, challenge = mark(handoff)
    sql = {
        "pause": "UPDATE lead.agents SET desired_state='paused'",
        "control": "UPDATE lead.agents SET control_version=control_version+1",
        "config": "UPDATE lead.agents SET config_revision=config_revision+1",
        "authorization": "UPDATE lead.agents SET authorization_version=authorization_version+1",
        "disable": "UPDATE lead.social_accounts SET automation_enabled=false",
    }
    with tx(actor.aid) as c:
        if change == "config":
            domain.snapshot_preferences(c, actor.aid)
        else:
            c.execute(sql[change])
    assert (
        challenges.finish(actor, challenge["id"], {"external_account_id": actor.aid})[
            "resumed_tasks"
        ]
        == 0
    )
    assert not events
    with tx(actor.aid) as c:
        assert (
            one(c, "SELECT state FROM lead.tasks WHERE id=%s", (task["id"],))["state"]
            == "waiting_user"
        )


def test_wrong_account_or_lost_session_keeps_tasks_waiting(handoff):
    actor, _, _, events = handoff
    _, challenge = mark(handoff)
    with pytest.raises(ValueError, match="原任务不同"):
        challenges.finish(
            actor, challenge["id"], {"external_account_id": "someone_else"}
        )
    actor.session = uid()
    with pytest.raises(ValueError, match="窗口已失效"):
        challenges.finish(actor, challenge["id"], {"external_account_id": actor.aid})
    with tx(actor.aid) as c:
        assert challenges.pending(c)
    assert not events


async def test_queued_automation_rechecks_after_manual_handoff(handoff):
    actor, _, _, _ = handoff
    lock = browser.BrowserLock(actor.aid, actor.profile["id"])
    await lock.acquire()
    entered = []

    async def worker():
        async with lock:
            entered.append(True)

    waiting = asyncio.create_task(worker())
    await asyncio.sleep(0)
    mark(handoff)
    lock.release()
    with pytest.raises(ValueError, match="等待真人验证"):
        await waiting
    assert not entered and not lock.locked()


async def test_newly_dispatched_task_waits_without_creating_an_action(
    handoff, monkeypatch
):
    actor, _, _, events = handoff
    _, challenge = mark(handoff)
    queued = domain.queue_lab_publish(actor.aid, "Queued while verifying", uid())
    rpc = AsyncMock(side_effect=AssertionError("browser must not be called"))
    monkeypatch.setattr(activities, "browser_rpc", rpc)
    result = await inspect.unwrap(activities.execute_task)(
        {"agent_id": actor.aid, "task_id": str(queued["id"])}
    )
    assert result["state"] == "needs_verification"
    rpc.assert_not_called()
    with tx(actor.aid) as c:
        assert (
            one(
                c,
                "SELECT count(*) n FROM lead.actions WHERE task_id=%s",
                (queued["id"],),
            )["n"]
            == 0
        )
    assert (
        challenges.finish(actor, challenge["id"], {"external_account_id": actor.aid})[
            "resumed_tasks"
        ]
        == 2
    )
    assert len([e for e in events if e[0] == "task.reconcile"]) == 2


@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1", reason="real browser fixture"
)
@pytest.mark.parametrize("initial_login", [False, True])
async def test_connect_close_reopen_and_verify_use_original_session(
    handoff, monkeypatch, initial_login
):
    from lead_app import adapters, api

    original, _, action, _ = handoff
    gateway = browser.Gateway()
    actor = await gateway.get(original.aid)
    monkeypatch.setattr(browser, "gateway", gateway)

    async def rpc(path, method="POST", body=None, **kwargs):
        result = await browser.open_browser(actor.aid, browser.OpenRequest(**body))
        return SimpleNamespace(json=lambda: result)

    monkeypatch.setattr(api, "gateway", rpc)
    monkeypatch.setattr(
        adapters,
        "get_adapter",
        lambda _: SimpleNamespace(
            account=AsyncMock(return_value={"external_account_id": actor.aid})
        ),
    )
    try:
        await actor.page.set_content("<h1>Verify you are human</h1>")
        if initial_login:
            with tx(actor.aid) as c:
                c.execute(
                    "UPDATE lead.social_accounts SET external_account_id=NULL,state='pending'"
                )
        challenges.block(actor, "lab", str(action["id"]))
        with tx(actor.aid) as c:
            before = one(c, "SELECT control_version,desired_state FROM lead.agents")
        first = await api.connect(UUID(actor.aid), api.Connection(platform="lab"))
        assert await actor.page.locator("h1").inner_text() == "Verify you are human"
        with tx(actor.aid) as c:
            assert (
                one(c, "SELECT control_version,desired_state FROM lead.agents")
                == before
            )
        api.end_takeover(UUID(actor.aid))
        stale = browser.ChallengeRequest(
            platform="lab",
            takeover=first["takeover_id"],
            challenge_id=first["challenge"]["id"],
        )
        with pytest.raises(Exception, match="接管"):
            await browser.verify_challenge(actor.aid, stale)
        second = await api.connect(UUID(actor.aid), api.Connection(platform="lab"))
        assert first["takeover_id"] != second["takeover_id"]
        assert second["challenge"]["session_id"] == str(actor.session)
        await actor.page.set_content("<h1>Ready</h1>")
        result = await browser.verify_challenge(
            actor.aid,
            browser.ChallengeRequest(
                platform="lab",
                takeover=second["takeover_id"],
                challenge_id=second["challenge"]["id"],
            ),
        )
        assert result["resumed_tasks"] == (0 if initial_login else 1)
        assert result["finish_login"] == initial_login
        with tx(actor.aid) as c:
            state = one(
                c,
                "SELECT state FROM lead.takeover_sessions WHERE id=%s",
                (second["takeover_id"],),
            )["state"]
            assert state == ("active" if initial_login else "ended")
    finally:
        await gateway.close(actor.aid)


async def test_gateway_pins_page_blocks_automation_and_connect_does_not_navigate(
    handoff, monkeypatch
):
    actor, _, _, _ = handoff
    _, challenge = mark(handoff)
    actor.used = 0
    actor.research_pages = {}
    actor.page = SimpleNamespace(goto=AsyncMock())
    gateway = browser.Gateway()
    gateway.actors[actor.aid] = actor
    monkeypatch.setattr(browser, "gateway", gateway)
    assert gateway.keep_preparation(actor.aid, actor.session)
    with pytest.raises(ValueError, match="等待真人验证"):
        await gateway.get(actor.aid)
    await gateway.evict_idle()
    assert gateway.actors[actor.aid] is actor
    opened = await browser.open_browser(actor.aid, browser.OpenRequest(platform="lab"))
    assert opened["challenge"]["id"] == challenge["id"]
    actor.page.goto.assert_not_called()
    actor.disconnected = True
    with pytest.raises(ValueError, match="窗口已失效"):
        await gateway.get(actor.aid, allow_challenge=True)


async def test_verify_endpoint_requires_challenge_gone_and_original_identity(
    handoff, monkeypatch
):
    from lead_app import adapters

    actor, _, _, events = handoff
    _, challenge = mark(handoff)
    actor.page = object()
    adapter = SimpleNamespace(
        account=AsyncMock(return_value={"external_account_id": actor.aid})
    )
    monkeypatch.setattr(adapters, "get_adapter", lambda _: adapter)
    monkeypatch.setattr(browser.gateway, "get", AsyncMock(return_value=actor))
    monkeypatch.setattr(browser, "require_takeover", lambda *args: None)
    monkeypatch.setattr(browser, "challenge_profile", lambda *args: actor.profile["id"])
    from lead_app.adapters import core

    check = AsyncMock(side_effect=HumanVerificationRequired("仍需验证"))
    monkeypatch.setattr(core, "check_challenge", check)
    body = browser.ChallengeRequest(
        platform="lab", takeover=UUID(uid()), challenge_id=UUID(challenge["id"])
    )
    with pytest.raises(HumanVerificationRequired):
        await browser.verify_challenge(actor.aid, body)
    adapter.account.assert_not_called()
    assert not events
    check.side_effect = None
    assert (await browser.verify_challenge(actor.aid, body))["resumed_tasks"] == 1
    assert (await browser.verify_challenge(actor.aid, body))["resumed_tasks"] == 1
    assert len([e for e in events if e[0] == "task.reconcile"]) == 1


@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1", reason="real browser fixture"
)
async def test_detection_with_real_browser_visible_hidden_login_and_post_content(
    handoff,
):
    actor, _, _, _ = handoff
    gateway = browser.Gateway()
    live = await gateway.get(actor.aid)
    try:

        async def route(request):
            await request.fulfill(
                body="<html><body></body></html>", content_type="text/html"
            )

        await live.page.route("https://www.reddit.com/**", route)
        await live.page.goto("https://www.reddit.com/")
        for html in (
            "<h1>Verify you are human</h1>",
            '<iframe title="Security challenge" src="about:blank"></iframe>',
            "<h2>真人验证</h2>",
        ):
            await live.page.set_content(html)
            with pytest.raises(HumanVerificationRequired):
                await check_challenge(live.page)
        await live.page.set_content(
            '<article><h1>Verify you are human</h1></article><iframe style="display:none" title="Security challenge"></iframe>'
        )
        await check_challenge(live.page)
        await live.page.set_content('<input type="password">')
        with pytest.raises(NeedsAuth) as error:
            await check_surface(live.page, {"www.reddit.com"})
        assert not isinstance(error.value, HumanVerificationRequired)
    finally:
        await gateway.close(actor.aid)


@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1", reason="real browser fixture"
)
@pytest.mark.parametrize("after_submit", [False, True])
async def test_publish_handoff_resumes_without_duplicate_submit(
    handoff, monkeypatch, after_submit
):
    from lead_app import social_publish

    original, task, action, events = handoff
    gateway = browser.Gateway()
    actor = await gateway.get(original.aid)
    submits = []
    preparations = []

    class Adapter:
        async def prepare(self, page, *args):
            preparations.append(1)
            try:
                await check_challenge(page)
            except HumanVerificationRequired as error:
                error.safe_to_restart = True
                raise
            return {"kind": "text"}

        async def before_commit(self, page, *args):
            await check_challenge(page)

        async def submit(self, page, observation):
            submits.append(1)
            if after_submit:
                await page.set_content("<h1>Verify you are human</h1>")

        async def receipt(self, *args):
            return {"external_id": "fixture-post-1"}

        async def reconcile(self, *args):
            assert len(submits) == 1
            return {"external_id": "fixture-post-1"}

    monkeypatch.setattr(social_publish, "get_adapter", lambda _: Adapter())
    try:
        await actor.page.set_content(
            "<h1>Ready</h1>" if after_submit else "<h1>Verify you are human</h1>"
        )
        result = await social_publish.execute(actor, str(action["id"]))
        assert result["state"] == "needs_verification"
        with tx(actor.aid) as c:
            challenge = challenges.pending(c)
            assert one(
                c, "SELECT state FROM lead.actions WHERE id=%s", (action["id"],)
            )["state"] == ("unknown" if after_submit else "prepared")
        assert len(submits) == int(after_submit)
        await actor.page.set_content("<h1>Ready</h1>")
        challenges.finish(actor, challenge["id"], {"external_account_id": actor.aid})
        assert (await social_publish.execute(actor, str(action["id"])))[
            "state"
        ] == "confirmed"
        assert len(submits) == 1
        assert len(preparations) == (1 if after_submit else 2)
        with tx(actor.aid) as c:
            assert (
                one(
                    c,
                    "SELECT count(*) n FROM lead.actions WHERE task_id=%s",
                    (task["id"],),
                )["n"]
                == 1
            )
    finally:
        await gateway.close(actor.aid)


async def test_challenge_appearing_between_dispatch_and_browser_access_is_resumable(
    handoff, monkeypatch
):
    actor, task, _, _ = handoff

    async def interrupted(*args, **kwargs):
        challenges.block(actor, "lab")
        raise RuntimeError("browser blocked by concurrent verification")

    monkeypatch.setattr(activities, "browser_rpc", interrupted)
    result = await inspect.unwrap(activities.execute_task)(
        {"agent_id": actor.aid, "task_id": str(task["id"])}
    )
    assert result["state"] == "needs_verification"
    with tx(actor.aid) as c:
        challenge = challenges.pending(c)
        saved = one(c, "SELECT state,result FROM lead.tasks WHERE id=%s", (task["id"],))
        assert saved["state"] == "waiting_user"
        assert saved["result"]["challenge_id"] == challenge["id"]
    assert (
        challenges.finish(actor, challenge["id"], {"external_account_id": actor.aid})[
            "resumed_tasks"
        ]
        == 1
    )


async def test_two_account_profiles_wait_and_resume_independently(handoff):
    from lead_app.account_profiles import for_platform

    actor, _, _, _ = handoff
    actors = []
    for platform in ("reddit", "x"):
        profile = for_platform(actor.aid, platform, create=True, isolated_login=True)
        with tx(actor.aid) as c:
            c.execute(
                "UPDATE lead.social_accounts SET external_account_id=%s,state='connected',automation_enabled=true WHERE profile_id=%s",
                (platform + "-owner", profile["id"]),
            )
        actors.append(
            SimpleNamespace(
                aid=actor.aid, profile=profile, session=uid(), disconnected=False
            )
        )
    challenges.block(actors[0], "reddit")
    # An unrelated account still acquires its browser lock while Reddit waits.
    async with browser.BrowserLock(actor.aid, actors[1].profile["id"]):
        pass
    challenges.block(actors[1], "x")
    with tx(actor.aid) as c:
        first = challenges.pending(c, actors[0].profile["id"])
        second = challenges.pending(c, actors[1].profile["id"])
        assert first["id"] != second["id"]
    challenges.finish(actors[0], first["id"], {"external_account_id": "reddit-owner"})
    with tx(actor.aid) as c:
        assert challenges.pending(c, actors[0].profile["id"]) is None
        assert challenges.pending(c, actors[1].profile["id"])["id"] == second["id"]


def test_rebound_account_never_resumes_an_old_challenge(handoff):
    actor, _, _, _ = handoff
    _, challenge = mark(handoff)
    with tx(actor.aid) as c:
        c.execute(
            "UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s",
            (challenge["account_id"],),
        )
    with pytest.raises(ValueError, match="账号绑定已改变"):
        challenges.finish(actor, challenge["id"], {"external_account_id": actor.aid})


@pytest.mark.skipif(os.environ.get("LEAD_BROWSER_TESTS") != "1", reason="real browser fixture")
@pytest.mark.parametrize("close_draft", [False, True])
async def test_rule_check_tab_is_preserved_and_original_draft_restored(handoff, monkeypatch, close_draft):
    from lead_app import social_publish
    from lead_app.adapters.web import WebAdapter
    from lead_app.reddit_member import rules_digest

    original, task, action, _ = handoff
    gateway = browser.Gateway()
    actor = await gateway.get(original.aid)
    draft = actor.page
    rule_text = "Respect community rules and disclose your affiliation clearly."
    require_verification = True
    preparations, submits = [], []
    adapter = WebAdapter("reddit")
    monkeypatch.setattr(adapter, "account", AsyncMock(return_value={"external_account_id": actor.aid}))

    async def rules_route(route):
        await route.fulfill(content_type="text/html", body=(
            "<main><h1>Verify you are human</h1></main>" if require_verification
            else f"<main>{rule_text}</main>"
        ))

    await actor.context.route("https://www.reddit.com/r/fixture/about/rules/", rules_route)

    class Adapter:
        async def prepare(self, page, target, *args):
            preparations.append(1)
            await page.set_content('<textarea>Original prepared draft</textarea><button>Publish</button>')
            return {"kind": "text", "fields": [], "prepared_url": page.url,
                    "target_id": target["external_target_id"], "final_selectors": ["button"]}

        async def before_commit(self, page, target, observation, expected_account):
            await adapter.before_commit(page, {**target, "canonical_url": "https://www.reddit.com/r/fixture/",
                "verified_permissions": {"access_mode": "ordinary_member", "rules": {"sha256": rules_digest(rule_text)}}}, observation, expected_account)

        async def submit(self, page, observation):
            assert page is draft
            assert await page.locator("textarea").input_value() == "Original prepared draft"
            submits.append(1)

        async def receipt(self, *args):
            return {"external_id": "fixture-only-post"}

    monkeypatch.setattr(social_publish, "get_adapter", lambda _: Adapter())
    try:
        result = await social_publish.execute(actor, str(action["id"]))
        assert result["state"] == "needs_verification"
        challenge_page = actor.page
        assert challenge_page is not draft and not challenge_page.is_closed()
        assert await challenge_page.get_by_role("heading").inner_text() == "Verify you are human"
        assert await draft.locator("textarea").input_value() == "Original prepared draft"
        assert not submits and len(preparations) == 1
        with tx(actor.aid) as c:
            challenge = challenges.pending(c)
        require_verification = False
        await challenge_page.set_content(f"<main>{rule_text}</main>")
        await check_challenge(challenge_page)
        if close_draft:
            await draft.close()
            with pytest.raises(ValueError, match="原草稿窗口已关闭"):
                challenges.finish(actor, challenge["id"], {"external_account_id": actor.aid})
            with tx(actor.aid) as c:
                assert challenges.pending(c)
                assert one(c, "SELECT state FROM lead.tasks WHERE id=%s", (task["id"],))["state"] == "waiting_user"
            assert actor.page is challenge_page and not submits
        else:
            assert challenges.finish(actor, challenge["id"], {"external_account_id": actor.aid})["resumed_tasks"] == 1
            assert actor.page is draft
            assert (await social_publish.execute(actor, str(action["id"])))["state"] == "confirmed"
            assert len(preparations) == 1 and len(submits) == 1
    finally:
        await gateway.close(actor.aid)
