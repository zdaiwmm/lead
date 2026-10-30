"""Account-only onboarding; all external operations below are local fixtures."""

import json
import os
from pathlib import Path

import pytest
from lead_app import account_setup, channels, domain, telegram
from lead_app.browser import Gateway
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.fixture
def connected(monkeypatch):
    for module in (domain, channels, account_setup):
        monkeypatch.setattr(module, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        domain, "model_config", lambda: {"key": "test-only", "model": "fixture"}
    )
    aid = domain.create_agent()
    with tx(aid) as c:
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        account = one(
            c,
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state) VALUES(%s,%s,%s,'telegram','pending') RETURNING id",
            (current_tenant(), aid, profile["id"]),
        )
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs) VALUES(%s,%s,1,'An owner-provided business description','{}','[]')",
            (current_tenant(), aid),
        )
    identity = str(int(uid().replace("-", "")[:12], 16))
    channels.store_account(
        aid, "telegram", {"external_account_id": identity, "display_name": "Setup test"}
    )
    return aid, str(account["id"]), identity


def test_resume_requires_login_but_not_a_preexisting_channel(connected):
    aid, _, _ = connected
    with tx(aid) as c:
        assert not one(c, "SELECT id FROM lead.account_targets")
        assert not one(c, "SELECT id FROM lead.authorization_grants")
        assert not one(
            c, "SELECT id FROM lead.interaction_cards WHERE type='manage_account'"
        )
    domain.set_control(aid, "resume")
    detail = domain.agent_detail(aid)
    assert detail["desired_state"] == "running"
    assert detail["accounts"][0]["operational_ready"] is False
    with tx(aid) as c:
        delegation = one(
            c, "SELECT * FROM lead.interaction_cards WHERE type='manage_account'"
        )
        assert delegation["state"] == "accepted"
        assert delegation["payload"]["limits"] == {
            "publish_per_day": 3,
            "read_others": False,
        }
    domain.set_control(aid, "pause")
    domain.set_control(aid, "resume")
    with tx(aid) as c:
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.interaction_cards WHERE type='manage_account'",
            )["n"]
            == 1
        )


@pytest.mark.asyncio
async def test_account_preparation_autobinds_and_verifies_without_more_user_steps(
    connected,
):
    aid, account, _ = connected
    domain.set_control(aid, "resume")
    calls = []

    async def rpc(path, data):
        calls.append(path)
        assert path.endswith("/account/prepare")
        return {
            "state": "confirmed",
            "target": {
                "external_target_id": "-" + str(int(uid().replace("-", "")[:12], 16)),
                "canonical_url": "https://web.telegram.org/k/?account=2#-345",
                "display_name": "Owned fixture channel",
                "verified_permissions": ["publish"],
                "public_url": "https://t.me/owned_fixture",
                "target_type": "channel",
            },
        }

    result = await account_setup.prepare_accounts(aid, rpc)
    assert result["reason"] == "account_verifying"
    with tx(aid) as c:
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"]
            == "running"
        )
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.authorization_grants WHERE revoked_at IS NULL",
            )["n"]
            == 1
        )
        target = one(c, "SELECT * FROM lead.account_targets")
        assert str(target["account_id"]) == account
        assert not target["verified_permissions"].get("publishing_verified_formats")
        probe = one(
            c, "SELECT * FROM lead.tasks WHERE type='connection.verify_publish'"
        )
        assert probe["state"] == "queued" and probe["input"]["delegation_id"]
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.interaction_cards WHERE state='pending'",
            )["n"]
            == 0
        )
    # Re-entering while verification is queued does not create a second post/channel.
    assert await account_setup.prepare_accounts(aid, rpc) is None
    assert len(calls) == 1
    domain.set_control(aid, "pause")
    assert await account_setup.prepare_accounts(aid, rpc) is None


def test_delegation_is_bound_to_owner_account_and_running_state(connected):
    aid, account_id, _ = connected
    domain.set_control(aid, "resume")
    with tx(aid) as c:
        a = one(c, "SELECT * FROM lead.agents")
        account = one(c, "SELECT * FROM lead.social_accounts")
        card = one(
            c, "SELECT * FROM lead.interaction_cards WHERE type='manage_account'"
        )
        account_setup.require_delegation(c, a, account, str(card["id"]))
        for changed in (
            {**a, "desired_state": "paused"},
            {**a, "desired_state": "draft"},
        ):
            with pytest.raises(ValueError, match="授权已变化"):
                account_setup.require_delegation(c, changed, account, str(card["id"]))
        with pytest.raises(ValueError, match="授权已变化"):
            account_setup.require_delegation(
                c, a, {**account, "external_account_id": "different"}, str(card["id"])
            )
        c.execute(
            "UPDATE lead.interaction_cards SET state='canceled' WHERE id=%s",
            (card["id"],),
        )
        with pytest.raises(ValueError, match="授权已变化"):
            account_setup.require_delegation(c, a, account, str(card["id"]))
    assert account_id


def test_replacing_login_revokes_the_previous_accounts_targets_and_delegation(
    connected,
):
    aid, account, _ = connected
    domain.set_control(aid, "resume")
    binding = channels.store_target(
        aid,
        account,
        {
            "external_target_id": "-" + str(int(uid().replace("-", "")[:12], 16)),
            "canonical_url": "https://web.telegram.org/k/#-123",
            "display_name": "Previous account channel",
            "verified_permissions": ["publish"],
        },
    )
    channels.accept_card(aid, str(binding["card_id"]), 1, True)
    channels.store_account(
        aid,
        "telegram",
        {
            "external_account_id": str(int(uid().replace("-", "")[:12], 16)),
            "display_name": "Replacement account",
        },
    )
    with tx(aid) as c:
        assert not one(
            c, "SELECT id FROM lead.authorization_grants WHERE revoked_at IS NULL"
        )
        assert not one(c, "SELECT id FROM lead.account_targets WHERE state='active'")
        assert not one(
            c,
            "SELECT id FROM lead.interaction_cards WHERE type='manage_account' AND state='accepted'",
        )


@pytest.mark.asyncio
async def test_uncertain_preparation_reuses_the_same_durable_task(connected):
    aid, _, _ = connected
    domain.set_control(aid, "resume")
    seen = []

    async def uncertain(path, data):
        seen.append(data["task_id"])
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET result=%s WHERE id=%s",
                (j({"creation_state": "unknown"}), data["task_id"]),
            )
        return {"state": "unknown"}

    for _ in range(2):
        assert (await account_setup.prepare_accounts(aid, uncertain))["delay"] == 60
    assert seen[0] == seen[1]
    with tx(aid) as c:
        assert len(all_rows(c, "SELECT id FROM lead.tasks")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("original", ["prepared", "unknown", "identity_changed"])
async def test_saved_login_recovers_without_replacing_unknown_submissions(
    connected, original
):
    from lead_app.activities import prepare_action

    aid, account, identity = connected
    domain.set_control(aid, "resume")
    observed = {
        "external_target_id": "-" + str(int(uid().replace("-", "")[:12], 16)),
        "canonical_url": "https://web.telegram.org/k/?account=2#-123",
        "display_name": "Owned channel",
        "target_type": "channel",
        "verified_permissions": ["publish"],
        "public_url": "https://t.me/owned_fixture",
    }

    async def prepare_rpc(path, data):
        return {"state": "confirmed", "target": observed}

    await account_setup.prepare_accounts(aid, prepare_rpc)
    with tx(aid) as c:
        task = one(c, "SELECT * FROM lead.tasks WHERE type='connection.verify_publish'")
    action = prepare_action(aid, str(task["id"]))
    with tx(aid) as c:
        if original == "unknown":
            c.execute(
                "UPDATE lead.actions SET state='dispatching' WHERE id=%s",
                (action["id"],),
            )
            c.execute(
                "UPDATE lead.actions SET state='unknown' WHERE id=%s", (action["id"],)
            )
        c.execute(
            "UPDATE lead.tasks SET state='waiting_user',result=%s WHERE id=%s",
            (j({"error": "ACCOUNT_NEEDS_AUTH"}), task["id"]),
        )
        c.execute(
            "UPDATE lead.social_accounts SET state='needs_auth' WHERE id=%s", (account,)
        )

    async def login_rpc(path, data):
        assert path.endswith("/account/inspect")
        return {
            "external_account_id": "different"
            if original == "identity_changed"
            else identity
        }

    await account_setup.recover_saved_login(aid, login_rpc)
    with tx(aid) as c:
        saved = one(
            c, "SELECT state FROM lead.social_accounts WHERE id=%s", (account,)
        )["state"]
        ledger = one(c, "SELECT state FROM lead.actions WHERE id=%s", (action["id"],))[
            "state"
        ]
        status = one(c, "SELECT state FROM lead.tasks WHERE id=%s", (task["id"],))[
            "state"
        ]
        assert saved == (
            "needs_auth" if original == "identity_changed" else "connected"
        )
        assert ledger == (
            "unknown"
            if original == "unknown"
            else "prepared"
            if original == "identity_changed"
            else "canceled"
        )
        assert status == (
            "reconciling"
            if original == "unknown"
            else "waiting_user"
            if original == "identity_changed"
            else "canceled"
        )
        assert one(c, "SELECT count(*) AS n FROM lead.actions")["n"] == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="real Camoufox, local DOM fixture only",
)
@pytest.mark.parametrize(
    "failure", [None, "lost_create_receipt", "pause_before_create"]
)
async def test_browser_preparation_creates_once_and_preserves_slot(
    connected, monkeypatch, failure
):
    aid, _, identity = connected
    domain.set_control(aid, "resume")
    gateway = Gateway()
    try:
        actor = await gateway.get(aid)
        html = (
            (Path(__file__).parent / "fixtures/social/telegram-setup.html")
            .read_text()
            .replace("__IDENTITY__", identity)
        )
        await actor.page.route(
            "https://web.telegram.org/k/**",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )
        await actor.page.goto("https://web.telegram.org/k/?account=2")
        original_submit = telegram.submit_new_channel
        original_fill = telegram.fill_new_channel

        async def lose_receipt(page):
            await original_submit(page)
            raise ConnectionError("receipt lost after channel was created")

        async def pause_before_submit(page, setup):
            await original_fill(page, setup)
            domain.set_control(aid, "pause")

        if failure == "lost_create_receipt":
            monkeypatch.setattr(telegram, "submit_new_channel", lose_receipt)
        elif failure == "pause_before_create":
            monkeypatch.setattr(telegram, "fill_new_channel", pause_before_submit)

        async def rpc(path, data):
            return await account_setup.prepare_in_browser(actor, data["task_id"])

        if failure == "pause_before_create":
            with pytest.raises(ValueError, match="授权已变化"):
                await account_setup.prepare_accounts(aid, rpc)
            assert (
                await actor.page.evaluate("localStorage.getItem('create_count')")
                is None
            )
            return
        first = await account_setup.prepare_accounts(aid, rpc)
        if failure:
            assert first["reason"] == "account_preparation_unknown"
            monkeypatch.setattr(telegram, "submit_new_channel", original_submit)
            first = await account_setup.prepare_accounts(aid, rpc)
        assert first["reason"] == "account_verifying"
        assert await actor.page.evaluate("localStorage.getItem('create_count')") == "1"
        assert "account=2" in actor.page.url
        assert json.loads(
            await actor.page.evaluate("localStorage.getItem('setup_channel')")
        )["handle"].startswith("lead_")
    finally:
        await gateway.close(aid)


@pytest.mark.asyncio
async def test_fresh_launch_uses_a_new_durable_command_and_correlates_probe(connected):
    aid, account, identity = connected
    domain.set_control(aid, "resume")
    request = uid()
    launch = account_setup.request_fresh_channel(aid, request)
    assert (
        account_setup.request_fresh_channel(aid, request)["task_id"]
        == launch["task_id"]
    )
    with pytest.raises(ValueError, match="已有新频道"):
        account_setup.request_fresh_channel(aid, uid())
    with tx(aid) as c:
        task = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (launch["task_id"],))
        assert task["input"]["fresh_channel"]
        assert task["input"]["external_account_id"] == identity
        assert "AI" in task["input"]["about"]
    calls = []

    async def rpc(path, payload):
        calls.append((path, payload))
        assert path.endswith("/account/prepare")
        assert payload["task_id"] == launch["task_id"]
        return {
            "state": "confirmed",
            "target": {
                "external_target_id": "-" + str(int(uid().replace("-", "")[:12], 16)),
                "canonical_url": "https://web.telegram.org/k/#-345",
                "display_name": task["input"]["title"],
                "verified_permissions": ["publish"],
                "public_url": "https://t.me/" + task["input"]["handle"],
                "target_type": "channel",
            },
        }

    assert (await account_setup.prepare_requested_channel(aid, rpc))[
        "reason"
    ] == "account_verifying"
    assert await account_setup.prepare_requested_channel(aid, rpc) is None
    assert len(calls) == 1
    status = account_setup.launch_status(aid, launch["task_id"])
    assert status["result"]["acceptance_state"] == "awaiting_verification"
    assert len(status["publications"]) == 1
    assert status["publications"][0]["type"] == "connection.verify_publish"
    assert status["takeovers_since_start"] == 0
    with tx(aid) as c:
        assert (
            one(
                c, "SELECT input FROM lead.tasks WHERE type='connection.verify_publish'"
            )["input"]["bootstrap_run_id"]
            == launch["task_id"]
        )


def test_fresh_launch_requires_running_owner_account(connected):
    aid, _, _ = connected
    with pytest.raises(ValueError, match="先启动"):
        account_setup.request_fresh_channel(aid, uid())
