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


@pytest.mark.asyncio
async def test_read_only_visit_advances_to_another_due_group(workspace):
    aid, base = workspace
    member = {"membership_origin": "existing", "join_state": "confirmed",
              "history_observation": {"joined": True, "can_reply": True}}
    with tx(aid) as c:
        first = create_task(c, workspace, "-401", "succeeded", member, hours=9)
        second = create_task(c, workspace, "-402", "succeeded", member, hours=8)
        create_task(c, workspace, "-403", "running", {}, reply={"reply_to_mid": "100"})
        owner = require_agent(c, aid)
        config = community_growth.discovery_config(c, owner)
        selected = community_selection.choose(c, owner, config, read_only=True)
    assert selected["kind"] == "review" and not selected["paid"]
    assert selected["membership_id"] == str(first["id"])
    operations = []

    async def observe(path, data):
        operations.append(data["operation"])
        now = datetime.now(timezone.utc).isoformat()
        with tx(aid) as c:
            c.execute("UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s",
                      (j({"target_id": "-401", "history_observed_at": now, "history_observation": {
                          "joined": True, "can_reply": True, "new_messages": 0}}), data["task_id"]))
        return {"target_id": "-401", "joined": True, "can_reply": True}

    result = await community_runtime.community_cycle(aid, observe, selection=selected)
    assert operations == ["observe"]
    assert result["reason"] == "community_readonly_observed"
    with tx(aid) as c:
        selected = community_selection.choose(c, owner, config, read_only=True)
        assert selected["membership_id"] == str(second["id"])


def test_standalone_permission_is_bound_to_group_evidence():
    candidate = {"evidence_id": "fixture", "rules_quote": "Members may showcase their projects here."}
    quote = candidate["rules_quote"]
    observed = {"description": "", "history": [{"mid": "4", "own": False}]}
    assert community_runtime.placement_evidence(candidate, observed, quote, "4")
    assert not community_runtime.placement_evidence(candidate, observed, "Ads welcome")
    assert not community_runtime.placement_evidence(candidate, observed, quote, "8")
    assert not community_runtime.placement_evidence(candidate, dict(observed, description="No ads"), quote)


@pytest.mark.parametrize("variant,reopens", [
    ("bound_read", True), ("platform_wait", False), ("uncertain_write", False),
    ("rate_limited", False), ("verification", False), ("new_failure", False),
    ("target_hold", False), ("custom_wait", False),
])
def test_successful_probe_reopens_only_legacy_account_read_hold(workspace, variant, reopens):
    aid, base = workspace
    last_read = datetime.now(timezone.utc) - timedelta(minutes=10)
    fault = {"error_code": "rate_limited" if variant == "rate_limited" else "browser_error",
             "retry_after": 600 if variant == "platform_wait" else None,
             "write_may_have_happened": variant == "uncertain_write"}
    state = {
        "type": community_health.TYPE, "state": "blocked", "account": base["external_account_id"],
        "peer": "-701" if variant == "target_hold" else "*", "error_code": fault["error_code"],
        "urls": ["https://t.me/fixture701"] if variant == "target_hold" else [],
        "fault": fault, "last_read_at": last_read.isoformat(),
        "last_failure_at": (last_read + timedelta(minutes=1 if variant == "new_failure" else -1)).isoformat(),
        "last_permission_evidence": {"joined": True, "can_reply": True, "verification_required": variant == "verification"},
        "read_after": (last_read + timedelta(hours=5 if variant == "custom_wait" else 6)).isoformat(),
        "write_after": (last_read + timedelta(hours=2)).isoformat(),
    }
    with tx(aid) as c:
        member = create_task(c, workspace, "-701", "succeeded", {"join_state": "confirmed",
            "membership_origin": "existing"}, hours=7)
        owner = require_agent(c, aid)
        config = community_growth.discovery_config(c, owner)
        key = uid()
        community_health._save(c, aid, key, state)
        for _ in range(2):
            selected = community_selection.choose(c, owner, config, read_only=True)
            assert (selected is not None) == reopens
            if reopens:
                assert selected["kind"] == "review" and selected["task_id"] == str(member["id"])
            record = one(c, "SELECT body FROM lead.memory_entries WHERE id=%s", (key,))["body"]
            assert record == {**state, "read_after": state["last_read_at"] if reopens else state["read_after"]}
            assert community_health.blocked(c, aid, member, write=True)


@pytest.mark.parametrize("variant,expects_probe", [
    ("receipt_timeout", True), ("future_read_after", False), ("explicit_platform_wait", False),
    ("unverified_membership", False), ("already_probed", False),
])
def test_recovering_account_reads_a_known_group_before_retrying_old_receipt(workspace, variant, expects_probe):
    aid, base = workspace
    now = datetime.now(timezone.utc)
    with tx(aid) as c:
        reliable = create_task(c, workspace, "-731", "succeeded", {
            "join_state": "confirmed", "membership_origin": "existing",
            "history_observed_at": (now - timedelta(minutes=8)).isoformat(),
            "history_observation": {"joined": variant != "unverified_membership", "new_messages": 0},
        }, hours=1)
        receipt = create_task(c, workspace, "-732", "reconciling", {"join_state": "unknown"}, hours=3)
        hold = {
            "type": community_health.TYPE, "account": base["external_account_id"], "peer": "*", "urls": [],
            "state": "blocked", "task_id": str(receipt["id"]), "error_code": "navigation_timeout",
            "fault": {"stage": "receipt", "retry_after": 600 if variant == "explicit_platform_wait" else None,
                      "write_may_have_happened": True},
            "last_failure_at": (now - timedelta(minutes=6)).isoformat(),
            "read_after": (now + timedelta(minutes=4 if variant == "future_read_after" else -1)).isoformat(),
            "write_after": (now + timedelta(hours=2)).isoformat(),
        }
        if variant == "already_probed":
            hold["last_read_at"] = (now - timedelta(minutes=2)).isoformat()
        community_health._save(c, aid, uid(), hold)
        owner = require_agent(c, aid)
        config = community_growth.discovery_config(c, owner)
        op = community_selection.choose(c, owner, config, read_only=True)
        assert bool(op and op["kind"] == "review") == expects_probe
        if expects_probe:
            assert op["task_id"] == str(reliable["id"]) and op["read_only"] and not op["paid"]
        assert community_health.blocked(c, aid, reliable, write=True)
        assert one(c, "SELECT state FROM lead.tasks WHERE id=%s", (receipt["id"],))["state"] == "reconciling"


@pytest.mark.asyncio
async def test_recovered_readonly_task_leaves_diagnostics(workspace):
    aid, _ = workspace
    now = datetime.now(timezone.utc)
    with tx(aid) as c:
        t = create_task(c, workspace, "-601", "failed", {"history_observation":{
            "joined":True,"can_reply":True}}, hours=8)
        c.execute("UPDATE lead.tasks SET input=input || %s WHERE id=%s", (j({"observation_only":True}),t["id"]))
        community_health.failed(c, aid, t, {"error_code":"navigation_timeout","stage":"observe",
                                          "retryable":True,"scope":"target"}, occurred_at=now-timedelta(hours=8))
        owner = require_agent(c, aid)
        config = community_growth.discovery_config(c, owner)
        op = community_selection.choose(c, owner, config, read_only=True)
    assert op["kind"] == "diagnose"

    async def observe(path, data):
        assert data["operation"] == "observe"
        with tx(aid) as c:
            c.execute("UPDATE lead.tasks SET result=result||%s WHERE id=%s", (j({"history_observed_at":now.isoformat()}),t["id"]))
        return {"target_id":"-601", "joined":True, "can_reply":True}

    result = await community_runtime.community_cycle(aid, observe, selection=op)
    assert result["reason"] == "community_readonly_observed"
    with tx(aid) as c:
        assert community_health.get(c, aid, t)["state"] == "healthy"
        assert community_selection.choose(c, owner, config, read_only=True) is None
