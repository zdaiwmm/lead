"""Authorization contracts; these do not constitute live Telegram acceptance."""

import json
import os

import pytest
from lead_app import channels, domain, telegram
from lead_app.activities import prepare_action
from lead_app.browser import Gateway
from lead_app.config import TENANT
from lead_app.db import one, tx, uid
from lead_app.telegram import channel_url


@pytest.fixture
def channel(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(channels, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    with tx(aid) as c:
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        account = one(
            c,
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state) VALUES(%s,%s,%s,'telegram','pending') RETURNING id",
            (TENANT, aid, profile["id"]),
        )
    channels.store_account(
        aid,
        "telegram",
        {
            "external_account_id": str(int(uid().replace("-", "")[:12], 16)),
            "display_name": "验收账号",
        },
    )
    target = channels.store_target(
        aid,
        str(account["id"]),
        {
            "external_target_id": "-" + str(int(uid().replace("-", "")[:12], 16)),
            "canonical_url": "https://web.telegram.org/k/#-123",
            "display_name": "自有验收频道",
            "verified_permissions": ["publish"],
            "public_url": "https://t.me/lead_test",
        },
    )
    yield aid, str(account["id"]), target
    domain.set_control(aid, "archive")


def test_target_scope_confirmation_idempotency_and_pause(channel, monkeypatch):
    aid, account, target = channel
    other = domain.create_agent()
    try:
        with pytest.raises(LookupError):
            channels.accept_card(other, str(target["card_id"]), 1, True)
        with pytest.raises(ValueError):
            channels.accept_card(aid, str(target["card_id"]), 2, True)
        with tx(aid) as c:
            assert not one(c, "SELECT id FROM lead.authorization_grants")
            assert (
                one(c, "SELECT lead.confirm_telegram_probe(%s) AS ok", (uid(),))["ok"]
                is False
            )
        channels.accept_card(aid, str(target["card_id"]), 1, True)
        assert channels.accept_card(aid, str(target["card_id"]), 1, True)["duplicate"]
        with tx(aid) as c:
            assert (
                one(c, "SELECT count(*) AS n FROM lead.authorization_grants")["n"] == 1
            )
            probe = one(
                c, "SELECT * FROM lead.interaction_cards WHERE type='verify_publish'"
            )
            assert probe["payload"]["marker"] in probe["payload"]["body"]
            assert not one(c, "SELECT id FROM lead.tasks")
        channels.accept_card(aid, str(probe["id"]), 1, True)
        channels.accept_card(aid, str(probe["id"]), 1, True)
        with tx(aid) as c:
            task = one(c, "SELECT * FROM lead.tasks")
            assert task["input"]["body"] == probe["payload"]["body"]
            assert one(c, "SELECT count(*) AS n FROM lead.tasks")["n"] == 1
        action = prepare_action(aid, str(task["id"]))
        assert action["intent"]["platform"] == "telegram"
        assert action["intent"]["body"] == probe["payload"]["body"]
        with tx(aid) as c:
            # An unsubmitted action must never promote a capability.
            assert (
                one(c, "SELECT lead.confirm_telegram_probe(%s) AS ok", (action["id"],))[
                    "ok"
                ]
                is False
            )
        assert domain.agent_detail(aid)["metrics"]["published"] == 0
        domain.set_control(aid, "pause")
        with tx(aid) as c:
            assert (
                one(c, "SELECT control_version FROM lead.agents")["control_version"]
                > action["expected_control_version"]
            )
    finally:
        domain.set_control(other, "archive")


def test_expired_or_declined_cards_cannot_authorize(channel):
    aid, _, target = channel
    channels.accept_card(aid, str(target["card_id"]), 1, False)
    with pytest.raises(ValueError):
        channels.accept_card(aid, str(target["card_id"]), 1, True)
    with tx(aid) as c:
        assert not one(c, "SELECT id FROM lead.authorization_grants")


def test_telegram_target_urls_are_narrow():
    assert (
        channel_url("https://t.me/lead_test")
        == "https://web.telegram.org/k/#@lead_test"
    )
    assert channel_url("@lead_test") == "https://web.telegram.org/k/#@lead_test"
    for value in [
        "http://t.me/lead_test",
        "https://evil.example/lead_test",
        "https://t.me/+invite",
        "https://t.me/user/123",
        "https://t.me@127.0.0.1/a",
    ]:
        with pytest.raises(ValueError):
            channel_url(value)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="real browser adapter contract, no external Telegram post",
)
async def test_telegram_adapter_preserves_account_slot_and_reconciles_without_resubmit(
    channel, monkeypatch
):
    aid, account_id, target = channel
    channels.accept_card(aid, str(target["card_id"]), 1, True)
    with tx(aid) as c:
        probe = one(
            c, "SELECT * FROM lead.interaction_cards WHERE type='verify_publish'"
        )
        account = one(
            c,
            "SELECT external_account_id FROM lead.social_accounts WHERE id=%s",
            (account_id,),
        )
        peer = one(
            c,
            "SELECT external_target_id FROM lead.account_targets WHERE id=%s",
            (target["target_id"],),
        )["external_target_id"]
        c.execute(
            "UPDATE lead.account_targets SET canonical_url=%s WHERE id=%s",
            ("https://web.telegram.org/k/?account=2#" + peer, target["target_id"]),
        )
    channels.accept_card(aid, str(probe["id"]), 1, True)
    with tx(aid) as c:
        task = one(c, "SELECT id FROM lead.tasks")
    action = prepare_action(aid, str(task["id"]))
    gateway = Gateway()
    # Official Web K DOM contract fixture, not live-channel acceptance evidence.
    html = """<!doctype html><div class="chatlist" style="width:100px;height:100px">Chats</div>
    <div class="chat-info"><span class="peer-title" data-peer-id="PEER">自有验收频道</span><span>2 subscribers</span></div>
    <div class="input-message-input" contenteditable="true" data-placeholder="Broadcast"></div><button class="btn-send">Send</button><div id="bubbles"></div>
    <script>
    localStorage.setItem('account1',JSON.stringify({userId:'11111'}));
    localStorage.setItem('account2',JSON.stringify({userId:'ACCOUNT'}));
    localStorage.setItem('user_auth',JSON.stringify({id:'11111'}));
    function render(){document.getElementById('bubbles').replaceChildren();JSON.parse(localStorage.getItem('fixture_posts')||'[]').forEach((body,i)=>{const el=document.createElement('div');el.className='bubble';el.dataset.mid=String(100+i);const msg=document.createElement('div');msg.className='message';msg.textContent=body;el.append(msg);document.getElementById('bubbles').append(el)});}
    render();document.querySelector('.btn-send').onclick=()=>{const posts=JSON.parse(localStorage.getItem('fixture_posts')||'[]');posts.push(document.querySelector('.input-message-input').textContent);localStorage.setItem('fixture_posts',JSON.stringify(posts));render();};
    </script>""".replace("PEER", peer).replace(
        "ACCOUNT", account["external_account_id"]
    )
    try:
        actor = await gateway.get(aid)
        await actor.page.route(
            "https://web.telegram.org/k/**",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )
        await actor.page.goto("https://web.telegram.org/k/?account=2#" + peer)
        own = await telegram.inspect_account(actor.page)
        assert (
            own["external_account_id"] == account["external_account_id"]
            and own["slot"] == "2"
        )
        original = telegram.receipt

        async def lose_receipt(*a, **k):
            raise ConnectionError("lost acknowledgement after submit")

        monkeypatch.setattr(telegram, "receipt", lose_receipt)
        first = await actor.publish_telegram(str(action["id"]))
        assert first["state"] == "unknown"
        monkeypatch.setattr(telegram, "receipt", original)
        second = await actor.publish_telegram(str(action["id"]))
        assert second["state"] == "confirmed"
        assert "?account=2" in actor.page.url
        posts = json.loads(
            await actor.page.evaluate("localStorage.getItem('fixture_posts')")
        )
        assert posts == [probe["payload"]["body"]]
        with tx(aid) as c:
            assert one(c, "SELECT count(*) AS n FROM lead.action_attempts")["n"] == 1
            assert domain.metrics(c)["published"] == 0
            before = one(
                c,
                "SELECT technical_state,adapter_version FROM lead.platform_capabilities WHERE platform='telegram' AND target_type='channel' AND action_type='publish'",
            )
            c.execute("SAVEPOINT verify_probe_contract")
            assert one(
                c, "SELECT lead.confirm_channel_probe(%s) AS ok", (action["id"],)
            )["ok"]
            permissions = one(
                c,
                "SELECT verified_permissions FROM lead.account_targets WHERE id=%s",
                (target["target_id"],),
            )["verified_permissions"]
            assert permissions["publishing_verified_formats"] == ["text"]
            c.execute("ROLLBACK TO SAVEPOINT verify_probe_contract")
            assert (
                one(
                    c,
                    "SELECT technical_state,adapter_version FROM lead.platform_capabilities WHERE platform='telegram' AND target_type='channel' AND action_type='publish'",
                )
                == before
            )
    finally:
        await gateway.close(aid)
