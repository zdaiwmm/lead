"""Account release, old-work fencing and re-binding with real scoped DB writes."""

import asyncio
import os
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from lead_app import account_setup, channels, domain, telegram
from lead_app import browser as browser_module
from lead_app.api import Connection, app, verify_login
from lead_app.browser import Actor, OpenRequest
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.scope import current_tenant, tenant_scope


def pending_account(aid, platform="telegram"):
    with tx(aid) as c:
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        return str(
            one(
                c,
                "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state) VALUES(%s,%s,%s,%s,'pending') RETURNING id",
                (current_tenant(), aid, profile["id"], platform),
            )["id"]
        )


@pytest.fixture
def bound(monkeypatch):
    for module in (domain, channels, account_setup):
        monkeypatch.setattr(module, "emit", lambda *a, **kw: None)
    aid = domain.create_agent()
    account_id = pending_account(aid)
    observed = {"external_account_id": uid(), "display_name": "解绑验收账号"}
    channels.store_account(aid, "telegram", observed)
    target = channels.store_target(
        aid,
        account_id,
        {
            "external_target_id": uid(),
            "canonical_url": "https://web.telegram.org/k/#-123",
            "display_name": "解绑验收频道",
            "verified_permissions": ["publish"],
        },
    )
    channels.accept_card(aid, str(target["card_id"]), 1, True)
    with tx(aid) as c:
        account = one(
            c, "SELECT * FROM lead.social_accounts WHERE id=%s", (account_id,)
        )
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        account_setup.delegate_accounts(c, aid)
    return aid, account, target, observed


@pytest.mark.parametrize("state", ["connected", "needs_auth", "restricted"])
def test_release_all_claiming_states_allows_another_agent_to_bind(bound, state):
    aid, account, target, observed = bound
    other = domain.create_agent()
    other_account = pending_account(other)
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.social_accounts SET state=%s WHERE id=%s",
            (state, account["id"]),
        )
    with pytest.raises(ValueError, match="另一名 Agent"):
        channels.store_account(other, "telegram", observed)
    channels.disconnect_account(aid, account["id"], account["verified_at"])
    assert channels.store_account(other, "telegram", observed) == other_account
    with tx(aid) as c:
        released = one(c, "SELECT * FROM lead.social_accounts")
        assert (
            released["state"] == "disconnected" and not released["automation_enabled"]
        )
        assert released["external_account_id"] == observed["external_account_id"]
        assert one(c, "SELECT state FROM lead.account_targets")["state"] == "disabled"
        assert not one(
            c, "SELECT id FROM lead.authorization_grants WHERE revoked_at IS NULL"
        )
        assert not one(
            c,
            "SELECT id FROM lead.interaction_cards WHERE state IN ('pending','accepted')",
        )
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"]
            == "running"
        )
    with tx(other) as c:
        rebound = one(c, "SELECT * FROM lead.social_accounts")
        assert rebound["state"] == "connected"
        assert rebound["profile_id"] != account["profile_id"]
        assert not one(c, "SELECT id FROM lead.authorization_grants")
        assert not one(c, "SELECT id FROM lead.account_targets")


def make_action(c, aid, target_id, state, payload=None):
    task = one(
        c,
        "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'fixture.publish','queued',%s,%s,now()) RETURNING id",
        (current_tenant(), aid, uid(), j(payload or {})),
    )
    agent = one(c, "SELECT * FROM lead.agents")
    grant = one(
        c, "SELECT id FROM lead.authorization_grants WHERE target_id=%s", (target_id,)
    )
    action = one(
        c,
        "INSERT INTO lead.actions(tenant_id,agent_id,task_id,target_id,grant_id,operation_key,type,intent,expected_control_version,expected_config_revision) VALUES(%s,%s,%s,%s,%s,%s,'publish',%s,%s,%s) RETURNING *",
        (
            current_tenant(),
            aid,
            task["id"],
            target_id,
            grant["id"],
            uid(),
            j({"platform": "telegram"}),
            agent["control_version"],
            agent["config_revision"],
        ),
    )
    if state != "prepared":
        c.execute(
            "UPDATE lead.actions SET state='dispatching' WHERE id=%s", (action["id"],)
        )
        if state == "confirmed":
            c.execute(
                "UPDATE lead.actions SET state='confirmed',external_id='fixture-receipt',confirmed_at=now() WHERE id=%s",
                (action["id"],),
            )
        else:
            c.execute(
                "UPDATE lead.actions SET state=%s WHERE id=%s", (state, action["id"])
            )
        if state == "confirmed":
            c.execute(
                "UPDATE lead.tasks SET state='succeeded' WHERE id=%s", (task["id"],)
            )
    return task["id"], action["id"]


def test_release_cancels_unsubmitted_work_and_preserves_receipts_and_other_channel(
    bound,
):
    aid, account, target, _ = bound
    with tx(aid) as c:
        before = one(c, "SELECT * FROM lead.agents")
        actions = {
            state: make_action(c, aid, target["target_id"], state)
            for state in (
                "prepared",
                "not_applied",
                "dispatching",
                "unknown",
                "confirmed",
            )
        }
        task_ids = []
        for payload in (
            {"platform": "telegram"},
            {"account_id": str(account["id"])},
            {"target_id": str(target["target_id"])},
            {"platform": "x"},
        ):
            task_ids.append(
                one(
                    c,
                    "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'fixture','waiting_user',%s,%s,now()) RETURNING id",
                    (current_tenant(), aid, uid(), j(payload)),
                )["id"]
            )
    channels.disconnect_account(aid, account["id"], account["verified_at"])
    with tx(aid) as c:
        after = one(c, "SELECT * FROM lead.agents")
        assert after["control_version"] == before["control_version"] + 1
        assert after["authorization_version"] == before["authorization_version"] + 1
        for state, (task_id, action_id) in actions.items():
            row = one(c, "SELECT * FROM lead.actions WHERE id=%s", (action_id,))
            assert row["state"] == (
                "canceled" if state in ("prepared", "not_applied") else state
            )
            assert one(c, "SELECT state FROM lead.tasks WHERE id=%s", (task_id,))[
                "state"
            ] == (
                "canceled"
                if state in ("prepared", "not_applied")
                else "succeeded"
                if state == "confirmed"
                else "queued"
            )
            if state == "confirmed":
                assert row["external_id"] == "fixture-receipt"
        assert [
            one(c, "SELECT state FROM lead.tasks WHERE id=%s", (tid,))["state"]
            for tid in task_ids
        ] == ["canceled", "canceled", "canceled", "waiting_user"]
        assert (
            len(
                all_rows(
                    c,
                    "SELECT id FROM lead.audit_events WHERE type='account.disconnected'",
                )
            )
            == 1
        )


def test_duplicate_release_is_safe_and_stale_confirmation_cannot_release_reconnected_account(
    bound,
):
    aid, account, _, observed = bound
    channels.disconnect_account(aid, account["id"], account["verified_at"])
    assert channels.disconnect_account(aid, account["id"], account["verified_at"])[
        "duplicate"
    ]
    channels.store_account(aid, "telegram", observed)
    with pytest.raises(ValueError, match="连接已更新"):
        channels.disconnect_account(aid, account["id"], account["verified_at"])
    with tx(aid) as c:
        rebound = one(c, "SELECT * FROM lead.social_accounts")
        assert rebound["state"] == "connected" and rebound["automation_enabled"]
        assert not one(
            c, "SELECT id FROM lead.authorization_grants WHERE revoked_at IS NULL"
        )


def test_release_rejects_other_agent_and_other_workspace(bound):
    aid, account, _, _ = bound
    other = domain.create_agent()
    with pytest.raises(LookupError, match="社媒账号不存在"):
        channels.disconnect_account(other, account["id"], account["verified_at"])
    with tenant_scope(uid()):
        with pytest.raises(LookupError, match="Agent 不存在"):
            channels.disconnect_account(aid, account["id"], account["verified_at"])


@pytest.mark.asyncio
async def test_delayed_login_verification_cannot_reclaim_a_released_account(
    bound, monkeypatch
):
    import lead_app.api as api_module

    aid, account, _, observed = bound

    async def inspect(*args, **kwargs):
        channels.disconnect_account(aid, account["id"], account["verified_at"])
        return httpx.Response(200, json=observed)

    monkeypatch.setattr(api_module, "gateway", inspect)
    with pytest.raises(ValueError, match="账号连接已变化"):
        await verify_login(UUID(aid), Connection(platform="telegram"))
    with tx(aid) as c:
        assert (
            one(c, "SELECT state FROM lead.social_accounts")["state"] == "disconnected"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [telegram.TelegramNeedsAuth, telegram.TelegramTargetMismatch]
)
async def test_late_browser_auth_error_cannot_reclaim_released_identity(
    bound, monkeypatch, error_type
):
    aid, account, target, observed = bound
    other = domain.create_agent()
    pending_account(other)
    with tx(aid) as c:
        _, action_id = make_action(c, aid, target["target_id"], "unknown")

    async def lost_session(*args):
        channels.disconnect_account(aid, account["id"], account["verified_at"])
        channels.store_account(other, "telegram", observed)
        raise error_type("fixture expired session")

    monkeypatch.setattr(telegram, "open_account", lost_session)
    actor = SimpleNamespace(aid=aid, lock=asyncio.Lock(), page=None, touch=lambda: None)
    assert (await Actor.publish_telegram(actor, str(action_id)))["state"] == "unknown"
    with tx(aid) as c:
        assert (
            one(c, "SELECT state FROM lead.social_accounts")["state"] == "disconnected"
        )


def test_disconnect_api_requires_account_permission_and_rejects_stale_request(
    bound, monkeypatch
):
    from lead_app import admin_permissions

    aid, account, _, _ = bound
    monkeypatch.setenv("LEAD_AUTH_MODE", "local_owner")
    path = f"/api/agents/{aid}/accounts/{account['id']}/disconnect"
    body = {"expected_verified_at": account["verified_at"].isoformat()}
    with TestClient(
        app, headers={"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}
    ) as client:
        assert client.post(path, json=body).status_code == 401
        assert client.post("/api/session", json={}).status_code == 200
        assert client.post(path, json={}).status_code == 422
        monkeypatch.setattr(
            admin_permissions,
            "resolve",
            lambda identity: {
                "role": "operator",
                "capabilities": [],
                "agent_ids": None,
                "access_resolved": True,
            },
        )
        assert client.post(path, json=body).status_code == 403
        monkeypatch.setattr(
            admin_permissions,
            "resolve",
            lambda identity: {
                "role": "operator",
                "capabilities": ["accounts.manage"],
                "agent_ids": None,
                "access_resolved": True,
            },
        )
        assert client.post(path, json={"expected_verified_at": None}).status_code == 409
        assert client.post(path, json=body).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("released", [True, False])
async def test_account_inspection_ignores_released_history_but_preserves_login_recovery(
    bound, monkeypatch, released
):
    from lead_app import adapters

    aid, account, _, observed = bound
    if released:
        channels.disconnect_account(aid, account["id"], account["verified_at"])
    else:
        with tx(aid) as c:
            c.execute("UPDATE lead.social_accounts SET state='needs_auth'")
    selected = []
    page = object()

    async def get_actor(requested):
        assert requested == aid
        return SimpleNamespace(lock=asyncio.Lock(), page=page)

    async def select_account(actual_page, identity):
        assert actual_page is page
        selected.append(identity)

    async def inspect_visible(actual_page):
        assert actual_page is page
        return {"external_account_id": "visible-new-account"}

    monkeypatch.setattr(browser_module.gateway, "get", get_actor)
    monkeypatch.setattr(telegram, "open_account", select_account)
    monkeypatch.setattr(
        adapters,
        "get_adapter",
        lambda platform: SimpleNamespace(account=inspect_visible),
    )
    assert await browser_module.inspect_account(
        UUID(aid), OpenRequest(platform="telegram")
    ) == {"external_account_id": "visible-new-account"}
    assert selected == ([] if released else [observed["external_account_id"]])


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="native Camoufox, disposable Telegram HTML and test DB only",
)
@pytest.mark.parametrize("slot", ["1", "2"])
@pytest.mark.parametrize("keep_old_login", [False, True])
async def test_release_then_verify_another_telegram_login_through_native_gateway(
    bound, monkeypatch, slot, keep_old_login
):
    import json

    from camoufox.async_api import AsyncCamoufox
    from lead_app import api as api_module
    from lead_app.config import BROWSER_BUILD

    aid, account, target, _ = bound
    old_id = str(int(uid().replace("-", "")[:12], 16))
    new_id = str(int(old_id) + 1)
    channels.store_account(
        aid,
        "telegram",
        {"external_account_id": old_id, "display_name": "Previous fixture"},
    )
    with tx(aid) as c:
        account = one(
            c, "SELECT * FROM lead.social_accounts WHERE id=%s", (account["id"],)
        )
    channels.disconnect_account(aid, account["id"], account["verified_at"])
    old_slot = "2" if slot == "1" else "1"
    storage = {"account" + slot: {"userId": new_id}}
    if keep_old_login:
        storage["account" + old_slot] = {"userId": old_id}
    async with AsyncCamoufox(browser=BROWSER_BUILD, headless=True) as native:
        page = await native.new_page()
        try:
            await page.add_init_script(
                "const logins="
                + json.dumps(storage)
                + ";localStorage.clear();for(const [key,value] of Object.entries(logins))localStorage.setItem(key,JSON.stringify(value));"
            )
            await page.route(
                "https://web.telegram.org/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body='<main class="chatlist" style="width:300px;height:400px">Signed-in fixture</main>',
                ),
            )
            await page.goto("https://web.telegram.org/k/?account=" + slot)
            actor = SimpleNamespace(page=page, lock=asyncio.Lock())

            async def get_actor(requested):
                assert requested == aid
                return actor

            async def inspect_gateway(path, body):
                assert path == f"/agents/{aid}/account/inspect"
                result = await browser_module.inspect_account(
                    UUID(aid), OpenRequest(**body)
                )
                return httpx.Response(200, json=result)

            monkeypatch.setattr(browser_module.gateway, "get", get_actor)
            monkeypatch.setattr(api_module, "gateway", inspect_gateway)
            result = await verify_login(UUID(aid), Connection(platform="telegram"))
            assert result["account_id"] == str(account["id"])
            assert page.url == "https://web.telegram.org/k/?account=" + slot
            with tx(aid) as c:
                current = one(
                    c,
                    "SELECT * FROM lead.social_accounts WHERE id=%s",
                    (account["id"],),
                )
                assert (
                    current["state"] == "connected"
                    and current["external_account_id"] == new_id
                )
                assert (
                    one(
                        c,
                        "SELECT state FROM lead.account_targets WHERE id=%s",
                        (target["target_id"],),
                    )["state"]
                    == "disabled"
                )
                assert not one(
                    c,
                    "SELECT id FROM lead.authorization_grants WHERE revoked_at IS NULL",
                )
            other = domain.create_agent()
            pending_account(other)
            channels.store_account(
                other,
                "telegram",
                {
                    "external_account_id": old_id,
                    "display_name": "Released identity fixture",
                },
            )
        finally:
            await page.close()
