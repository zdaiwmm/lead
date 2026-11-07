"""Regression cases for confirmed group sends and fair service with a receipt backlog."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from lead_app import (
    channels,
    community_growth,
    community_health,
    community_receipts,
    community_runtime,
    community_selection,
    domain,
    telegram,
)
from lead_app.db import j, one, require_agent, tx, uid
from lead_app.scope import current_tenant
from playwright.async_api import async_playwright


@pytest.fixture
def workspace(monkeypatch):
    for module in (channels, community_growth, domain):
        monkeypatch.setattr(module, "emit", lambda *args, **kwargs: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-fixture")
    monkeypatch.setenv("LEAD_MODEL_PROVIDER", "deepseek")
    aid = domain.create_agent()
    external = str(int(uid().replace("-", "")[:12], 16))
    with tx(aid) as c:
        account = one(
            c,
            """INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state)
            SELECT %s,%s,id,'telegram','pending' FROM lead.browser_profiles RETURNING id""",
            (current_tenant(), aid),
        )
    channels.store_account(
        aid, "telegram", {"external_account_id": external, "display_name": "Fixture"}
    )
    domain.set_control(aid, "resume")
    community_growth.set_strategy(
        aid,
        {"enabled": True, "mode": "contextual_outreach", "allow_proactive_post": True},
    )
    with tx(aid) as c:
        owner = require_agent(c, aid)
        card = one(
            c, "SELECT id FROM lead.interaction_cards WHERE type='manage_community'"
        )
    return aid, {
        "account_id": str(account["id"]),
        "delegation_id": str(card["id"]),
        "external_account_id": external,
        "control_version": owner["control_version"],
        "config_revision": owner["config_revision"],
    }


def create_task(c, workspace, peer, state, result, *, hours=0, reply=None):
    aid, base = workspace
    data = {
        **base,
        "history_adapter_version": community_runtime.HISTORY_ADAPTER_VERSION,
        "candidate": {"telegram_url": "https://t.me/fixture" + peer[1:]},
    }
    if reply is not None:
        data["reply"] = reply
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    return one(
        c,
        """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,created_at,scheduled_at,completed_at)
        VALUES(%s,%s,'community.engage',%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (
            current_tenant(),
            aid,
            state,
            uid(),
            j(data),
            j({"target_id": peer, **result}),
            when,
            when,
            when if state in ("failed", "succeeded") else None,
        ),
    )


def test_old_receipts_and_due_diagnostics_yield_to_healthy_groups(workspace):
    aid, account = workspace
    now = datetime.now(timezone.utc)
    with tx(aid) as c:
        owner = require_agent(c, aid)
        config = community_growth.GrowthConfig.model_validate(
            owner["onboarding_state"]["community_growth"]
        )
        member = {
            "membership_origin": "existing",
            "join_state": "confirmed",
            "history_observation": {"joined": True, "can_reply": True},
        }
        broken = create_task(c, workspace, "-100", "succeeded", member, hours=48)
        fault = {
            "error_code": "navigation_timeout",
            "scope": "target",
            "retryable": True,
            "stage": "observe",
        }
        community_health.failed(
            c, aid, broken, fault, occurred_at=now - timedelta(hours=4)
        )
        healthy = [
            create_task(c, workspace, str(-200 - i), "succeeded", member, hours=30 + i)
            for i in range(16)
        ]
        for i in range(21):
            create_task(
                c,
                workspace,
                str(-300 - i),
                "reconciling",
                {
                    **(
                        {"join_state": "unknown"}
                        if i < 13
                        else {"reply_state": "unknown"}
                    ),
                    "receipt_attempted_at": now.isoformat(),
                    "execution_steps": [
                        {
                            "phase": "browser_observe",
                            "duration_ms": 1000,
                            "at": now.isoformat(),
                            "state": "failed",
                        }
                    ],
                },
                hours=20,
            )
        assert community_selection.recovery_share(c, account["account_id"]) == 1
        for expected in reversed(healthy):
            choice = community_selection.choose(c, owner, config, now)
            assert (
                choice["kind"] == "review"
                and choice["task_id"] == str(expected["id"])
                and choice["overdue"]
            )
            # Simulate the new successful membership visit that advances that group's clock.
            create_task(
                c,
                workspace,
                expected["result"]["target_id"],
                "succeeded",
                {
                    **member,
                    "decision": {"proactive_reviewed": True, "ad_feed_revision": "2"},
                },
            )
        assert community_selection.choose(c, owner, config, now)["kind"] == "receipt"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("LEAD_BROWSER_TESTS") != "1", reason="requires an isolated local browser"
)
@pytest.mark.parametrize(
    "engine",
    ["chromium", *(["camoufox"] if os.getenv("LEAD_FIXTURE_CAMOUFOX") else [])],
)
@pytest.mark.parametrize(
    "case",
    [
        "reply",
        "promotion",
        "lost_ack",
        "failed",
        "foreign_draft",
        "wrong_peer",
        "old_failure",
        "concurrent",
        "preclick_crash",
    ],
)
async def test_native_submit_and_recovery(workspace, monkeypatch, case, engine):
    aid, base = workspace
    peer = "-737"
    original = "Which tool can I use?"
    body = "Line one\nA useful example" if case != "reply" else "Here is an example."
    standalone = case != "reply"
    intent = {
        "body": body,
        "promote": standalone,
        "standalone": standalone,
        "reply_to_mid": None if standalone else "100",
        "placement": "advertising_group" if standalone else None,
        "source_hash": community_runtime.digest(original),
        "content_hash": community_runtime.digest(body),
    }
    with tx(aid) as c:
        task = create_task(c, workspace, peer, "running", {}, reply=intent)
    tid = str(task["id"])
    async with async_playwright() as pw:
        if engine == "camoufox":
            browser = await pw.firefox.launch(
                executable_path=os.environ["LEAD_FIXTURE_CAMOUFOX"]
            )
        else:
            browser = await pw.chromium.launch(
                channel="chromium", executable_path=os.getenv("LEAD_FIXTURE_CHROMIUM")
            )
        page = await browser.new_page()
        try:
            html = """<style>[hidden]{display:none!important} [contenteditable]{min-height:20px}</style>
            <div class="chatlist">Chats</div><div class="chat active" data-type="chat">
            <div class="chat-info"><span class="peer-title" data-peer-id="-737">Fixture</span></div>
            <div class="bubble" data-mid="100"><div class="message">Which tool can I use?</div></div>
            <button class="btn-menu-item" hidden>Reply</button><div class="reply-wrapper" hidden>Which tool can I use?</div>
            <div class="input-message-input" contenteditable="true"></div><button class="btn-send send">Send</button></div>
            <script>const q=s=>document.querySelector(s); document.body.dataset.clicks='0';
            q('.bubble').oncontextmenu=e=>{e.preventDefault();q('.btn-menu-item').hidden=false};
            q('.btn-menu-item').onclick=()=>{q('.reply-wrapper').hidden=false;q('.btn-menu-item').hidden=true};
            q('.btn-send').onclick=()=>{document.body.dataset.clicks=String(Number(document.body.dataset.clicks)+1);const m=document.createElement('div');m.className='message';
            m.innerText=q('[contenteditable]').innerText;const b=document.createElement('div');b.className='bubble is-out';
            b.dataset.mid='101';if(document.body.dataset.failSend)b.classList.add('is-error');b.append(m);q('.chat').append(b);
            q('[contenteditable]').textContent='';q('.reply-wrapper').hidden=true};</script>"""
            await page.route(
                "**/*", lambda route: route.fulfill(body=html, content_type="text/html")
            )
            await page.goto("https://web.telegram.org/k/")
            await page.evaluate(
                "(id)=>localStorage.setItem('account1',JSON.stringify({userId:id}))",
                base["external_account_id"],
            )
            actor = SimpleNamespace(
                aid=aid,
                page=page,
                lock=asyncio.Lock(),
                touch=lambda: None,
                gate=lambda *args, **kwargs: None,
            )

            async def observe(*args):
                return {
                    "target_id": peer,
                    "joined": True,
                    "can_reply": True,
                    "description": "Advertising allowed",
                    "history": [{"mid": "100", "own": False, "body": original}],
                    "history_source": "recent",
                }

            monkeypatch.setattr(community_runtime, "observe_group", observe)
            if case == "old_failure":
                await page.evaluate(
                    """body => {
                    const old = document.createElement('div');
                    old.innerHTML='<div class="bubble is-out is-error" data-mid="100.1"><div class="message"></div></div>';
                    old.querySelector('.message').innerText=body;
                    document.querySelector('.chat').append(old);
                }""",
                    body,
                )
            if case == "foreign_draft":
                await page.locator("[contenteditable]").fill("keep this draft")
                assert (await community_runtime.browser_step(actor, tid, "reply"))[
                    "state"
                ] == "blocked"
                assert (
                    await page.locator("[contenteditable]").inner_text()
                    == "keep this draft"
                )
                assert await page.evaluate("Number(document.body.dataset.clicks)") == 0
                return
            if case == "wrong_peer":
                await page.locator(".peer-title").evaluate("e=>e.dataset.peerId='-738'")
                with pytest.raises(telegram.TelegramTargetMismatch):
                    await community_runtime.browser_step(actor, tid, "reply")
                assert await page.evaluate("Number(document.body.dataset.clicks)") == 0
                return
            if case == "failed":
                await page.evaluate("document.body.dataset.failSend='1'")
            submit = telegram.submit
            if case == "lost_ack":

                async def lose_response(page, **kwargs):
                    await submit(page, **kwargs)
                    raise ConnectionError("lost response")

                monkeypatch.setattr(telegram, "submit", lose_response)
                with pytest.raises(ConnectionError):
                    await community_runtime.browser_step(actor, tid, "reply")
                monkeypatch.setattr(telegram, "submit", submit)
            if case == "preclick_crash":

                async def never_clicked(page, **kwargs):
                    raise ConnectionError("crashed at the committed dispatch boundary")

                async def no_history(*args):
                    return [], "native_group_search"

                monkeypatch.setattr(telegram, "submit", never_clicked)
                monkeypatch.setattr(community_runtime, "search_history", no_history)
                with pytest.raises(ConnectionError):
                    await community_runtime.browser_step(actor, tid, "reply")
                monkeypatch.setattr(telegram, "submit", submit)
                assert (await community_runtime.browser_step(actor, tid, "reply"))[
                    "state"
                ] == "unknown"
                assert await page.evaluate("Number(document.body.dataset.clicks)") == 0
                return
            if case == "concurrent":
                together = await asyncio.gather(
                    community_runtime.browser_step(actor, tid, "reply"),
                    community_runtime.browser_step(actor, tid, "reply"),
                )
                assert all(item["state"] == "confirmed" for item in together)
            result = await community_runtime.browser_step(actor, tid, "reply")
            if case == "failed":
                assert result["state"] == "failed"
            else:
                assert result == {"state": "confirmed", "external_id": peer + ":101"}
            with tx(aid) as c:
                current = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (tid,))
                assert current["result"]["reply_state"] == result["state"]
                # An older pending read cannot overwrite a terminal receipt.
                assert (
                    community_receipts.save_message(
                        c, aid, current, peer, {"state": "unknown"}
                    )["state"]
                    == result["state"]
                )
            await community_runtime.browser_step(actor, tid, "reply")
            assert await page.evaluate("Number(document.body.dataset.clicks)") == 1
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["resume", "receipt"])
async def test_failed_delivery_cycle_closes_task_as_failed(workspace, kind):
    aid, _ = workspace
    with tx(aid) as c:
        result = {"reply_state": "dispatching"} if kind == "receipt" else {}
        task = create_task(
            c,
            workspace,
            "-655",
            "running",
            result,
            reply={"promote": False, "body": "example"},
        )

    async def client_failure(path, data):
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
                (j({"reply_state": "failed"}), task["id"]),
            )
        return {"state": "failed", "reason": "client delivery failed"}

    await community_runtime.community_cycle(
        aid, client_failure, selection={"kind": kind, "task_id": str(task["id"])}
    )
    with tx(aid) as c:
        current = one(
            c,
            "SELECT state,completed_at,result FROM lead.tasks WHERE id=%s",
            (task["id"],),
        )
        assert current["state"] == current["result"]["outcome"]["state"] == "failed"
        assert current["completed_at"] is not None


def test_read_timeout_does_not_reopen_failed_writes(workspace):
    aid, _ = workspace
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    observation = {"joined": True, "can_reply": True, "verification_required": False}
    with tx(aid) as c:
        task = create_task(
            c,
            workspace,
            "-711",
            "failed",
            {"reply_state": "failed", "history_observation": observation},
        )
        community_health.failed(
            c,
            aid,
            task,
            {"error_code": "send_failed", "stage": "reply"},
            occurred_at=two_hours_ago,
        )
        fault = {
            "error_code": "navigation_timeout",
            "stage": "observe",
            "evidence_ref": "attempt",
        }
        state = community_health.failed(c, aid, task, fault)
        assert state["error_code"] == "send_failed" and state["failure_count"] == 2
        assert datetime.fromisoformat(state["read_after"]) > datetime.now(
            timezone.utc
        ) + timedelta(hours=1)
        community_health.observed(c, aid, task, observation)
        assert community_health.blocked(c, aid, task)["error_code"] == "send_failed"
