import json
import os

import pytest
from lead_app import channels, community_growth, community_runtime, domain, telegram
from lead_app.browser import Gateway
from lead_app.db import j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,closed,settles",
    [
        ("NS_BINDING_ABORTED; frame changed", False, True),
        ("NS_BINDING_ABORTED", True, False),
        ("network failure", False, False),
    ],
)
async def test_reload_settles_only_known_navigation_abort_then_rechecks_identity(
    monkeypatch, message, closed, settles
):
    from playwright.async_api import Error as BrowserError

    calls = []

    class Page:
        async def reload(self, **kwargs):
            calls.append("reload")
            raise BrowserError(message)

        def is_closed(self):
            return closed

        async def wait_for_load_state(self, *args, **kwargs):
            calls.append("settle")

    async def identity(page, expected):
        assert expected == "fixture-account"
        calls.append("identity")

    monkeypatch.setattr(telegram, "open_account", identity)
    if settles:
        await community_runtime.reload_telegram(Page(), "fixture-account")
        assert calls == ["reload", "settle", "identity"]
    else:
        with pytest.raises(BrowserError):
            await community_runtime.reload_telegram(Page(), "fixture-account")
        assert calls == ["reload"]


def test_membership_requires_join_control_to_disappear_and_no_pending_request():
    preview = {
        "join_button_count": 1,
        "controls": [],
        "editor_metadata": [{"editable": True}],
    }
    assert not community_runtime.native_membership(preview)
    joined_muted = {
        **preview,
        "join_button_count": 0,
        "editor_metadata": [{"editable": False}],
    }
    assert community_runtime.native_membership(joined_muted)
    assert not community_runtime.native_membership(
        {**joined_muted, "controls": ["CANCEL REQUEST"]}
    )
    assert not community_runtime.native_membership(
        {**joined_muted, "controls": ["SUBSCRIBE"]}
    )
    assert not community_runtime.native_membership(
        {**joined_muted, "apply_button_count": 1}
    )
    assert community_runtime.pending_join_request({"controls": ["CANCEL REQUEST"]})
    assert not community_runtime.pending_join_request(
        {"controls": ["APPLY TO JOIN GROUP"]}
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="isolated browser and local fixture only",
)
async def test_product_community_join_history_contextual_reply_and_lost_receipt(
    monkeypatch,
):
    for module in (domain, channels, community_growth):
        monkeypatch.setattr(module, "emit", lambda *a, **k: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-only")
    monkeypatch.setenv("LEAD_MODEL_PROVIDER", "deepseek")
    aid = domain.create_agent()
    identity = str(int(uid().replace("-", "")[:12], 16))
    with tx(aid) as c:
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        account = one(
            c,
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state) VALUES(%s,%s,%s,'telegram','pending') RETURNING id",
            (current_tenant(), aid, profile["id"]),
        )
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs) VALUES(%s,%s,1,'EPUB translation and terminology','{}','[]')",
            (current_tenant(), aid),
        )
    channels.store_account(
        aid,
        "telegram",
        {"external_account_id": identity, "display_name": "Fixture account"},
    )
    domain.set_control(aid, "resume")
    community_growth.set_strategy(
        aid, {"enabled": True, "mode": "contextual_outreach", "max_joins_per_day": 3}
    )
    with tx(aid) as c:
        a = one(c, "SELECT * FROM lead.agents")
        card = one(
            c, "SELECT id FROM lead.interaction_cards WHERE type='manage_community'"
        )
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,scheduled_at,workflow_id) VALUES(%s,%s,'community.engage','running',%s,now(),%s) RETURNING id",
            (
                current_tenant(),
                aid,
                j(
                    {
                        "account_id": str(account["id"]),
                        "external_account_id": identity,
                        "delegation_id": str(card["id"]),
                        "control_version": a["control_version"],
                        "config_revision": a["config_revision"],
                        "candidate": {"telegram_url": "https://t.me/readers_fixture"},
                    }
                ),
                "fixture:" + uid(),
            ),
        )
    html = """<!doctype html><style>[hidden]{display:none!important}.chatlist{width:100px;height:100px}[contenteditable]{min-height:50px}</style>
    <div class="chatlist">Chats</div><div class="chat-info"><span class="peer-title" data-peer-id="-777777">Readers</span>4 members</div>
    <div id="column-right" inert><div class="profile-container active"><div class="sidebar-header"><button class="sidebar-close-button">Close</button></div><div class="row row-grid"><div class="row-title">t.me/readers_fixture</div></div></div></div>
    <button id="join">Join Group</button><div class="input-message-input" contenteditable="true" data-placeholder="Message" hidden></div><button class="btn-send send">Send</button>
    <div class="bubble" data-mid="100"><div class="message">How to keep an EPUB glossary consistent?</div></div><button class="btn-menu-item" hidden>Reply</button><div class="reply-wrapper" hidden>How to keep an EPUB glossary consistent?</div><div id="posts"></div>
    <script>localStorage.setItem('account1',JSON.stringify({userId:'IDENTITY'}));
    const q=s=>document.querySelector(s);q('.chat-info').onclick=()=>{delete q('.profile-container').dataset.closeStep;q('#column-right').inert=false;document.body.classList.add('is-right-column-shown')};
    q('.sidebar-close-button').onclick=()=>{if(!q('.profile-container').dataset.closeStep){q('.profile-container').dataset.closeStep='1';return}q('#column-right').inert=true;document.body.classList.remove('is-right-column-shown')};
    q('#join').onclick=()=>{q('#join').hidden=true;q('.input-message-input').hidden=false;localStorage.setItem('join_count',String(Number(localStorage.getItem('join_count')||0)+1))};
    q('.bubble').oncontextmenu=e=>{e.preventDefault();q('.btn-menu-item').hidden=false};q('.btn-menu-item').onclick=()=>{q('.btn-menu-item').hidden=true;q('.reply-wrapper').hidden=false};
    q('.btn-send').onclick=()=>{const body=q('.input-message-input').textContent;const posts=JSON.parse(localStorage.getItem('posts')||'[]');posts.push(body);localStorage.setItem('posts',JSON.stringify(posts));const e=document.createElement('div');e.className='bubble is-out';e.dataset.mid=String(100+posts.length);const m=document.createElement('div');m.className='message';m.textContent=body;e.append(m);q('#posts').append(e);q('.input-message-input').textContent=''};</script>""".replace(
        "IDENTITY", identity
    )
    gateway = Gateway()
    try:
        actor = await gateway.get(aid)
        await actor.page.route(
            "https://web.telegram.org/k/**",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )
        await actor.page.goto("https://web.telegram.org/k/")
        observed = await community_runtime.browser_step(
            actor, str(task["id"]), "observe"
        )
        assert observed["kind"] == "discussion" and not observed["joined"]
        # Telegram may canonicalize the public username route to a peer ID.
        # An already bound visible peer must not be closed and opened again.
        await actor.page.evaluate("history.replaceState(null,'','#-777777')")
        from lead_app import model

        async def relevant_community(output, *args, **kwargs):
            assert output is community_runtime.CommunityAdmission
            return output(
                community_relevant=True,
                reason="Relevant reading community; no unanswered question currently.",
            )

        async def product_rpc(path, data):
            return await community_runtime.browser_step(
                actor, data["task_id"], data["operation"]
            )

        with monkeypatch.context() as patch:
            patch.setattr(model, "run_model", relevant_community)
            result = await community_runtime.community_cycle(aid, product_rpc)
            assert result["reason"] == "community_joined"
        assert actor.page.url.endswith("#-777777")
        assert (await community_runtime.browser_step(actor, str(task["id"]), "join"))[
            "state"
        ] == "confirmed"
        assert (await community_runtime.browser_step(actor, str(task["id"]), "join"))[
            "state"
        ] == "confirmed"
        observed = await community_runtime.browser_step(
            actor, str(task["id"]), "observe"
        )
        assert observed["joined"] and len(observed["history"]) == 1
        original = observed["history"][0]
        body = "先固定术语表，再检查每一章相同术语的译法。"
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET input=input || %s WHERE id=%s",
                (
                    j(
                        {
                            "reply": {
                                "body": body,
                                "reply_to_mid": original["mid"],
                                "source_hash": community_runtime.digest(
                                    original["body"]
                                ),
                                "content_hash": community_runtime.digest(body),
                                "promote": False,
                            }
                        }
                    ),
                    task["id"],
                ),
            )
        submit = telegram.submit

        async def lose_ack(page):
            await submit(page)
            raise ConnectionError("ack lost")

        monkeypatch.setattr(telegram, "submit", lose_ack)
        await actor.page.evaluate(
            """() => {const original=document.querySelector('.bubble');original.remove();window.addEventListener('hashchange',()=>{if(location.hash.includes('?message=100'))document.body.append(original)})}"""
        )
        with pytest.raises(ConnectionError):
            await community_runtime.browser_step(actor, str(task["id"]), "reply")
        monkeypatch.setattr(telegram, "submit", submit)
        await actor.page.evaluate(
            """body => {const e=document.createElement('div');e.className='bubble is-out is-sending';e.dataset.mid='100.0001';const m=document.createElement('div');m.className='message';m.textContent=body;e.append(m);document.body.append(e)}""",
            body,
        )
        assert (await community_runtime.browser_step(actor, str(task["id"]), "reply"))[
            "state"
        ] == "confirmed"
        assert json.loads(
            await actor.page.evaluate("localStorage.getItem('posts')")
        ) == [body]
        assert await actor.page.evaluate("localStorage.getItem('join_count')") == "1"
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET state='succeeded',completed_at=now() WHERE id=%s",
                (task["id"],),
            )
            assert community_runtime.membership_review(c)["membership_id"] == task["id"]
            c.execute(
                "UPDATE lead.tasks SET input=input || %s WHERE id=%s",
                (
                    j(
                        {
                            "history_adapter_version": community_runtime.HISTORY_ADAPTER_VERSION
                        }
                    ),
                    task["id"],
                ),
            )
            assert community_runtime.membership_review(c) is None
            c.execute(
                "UPDATE lead.tasks SET completed_at=now()-interval '2 hours' WHERE id=%s",
                (task["id"],),
            )
            review = community_runtime.membership_review(c)
            assert review["id"] == task["id"]
            assert review["result"]["reply_state"] == "confirmed"
        from httpx import ConnectError
        from lead_app import model

        async def temporarily_unavailable(*args, **kwargs):
            raise ConnectError("fixture service restart")

        result = await community_runtime.community_cycle(aid, temporarily_unavailable)
        assert result["reason"] == "community_service_recovery"
        with tx(aid) as c:
            pending = one(
                c,
                "SELECT id,state,result FROM lead.tasks WHERE type='community.engage' ORDER BY created_at DESC LIMIT 1",
            )
            assert pending["state"] == "running"
            assert not pending["result"].get("join_submitted")

        async def no_new_question(*args, **kwargs):
            return community_runtime.CommunityDecision(
                reason="No new unanswered question."
            )

        async def fixture_rpc(path, data):
            return await community_runtime.browser_step(
                actor, data["task_id"], data["operation"]
            )

        async def model_timeout(*args, **kwargs):
            raise TimeoutError("isolated provider timeout")

        monkeypatch.setattr(model, "run_model", model_timeout)
        await community_runtime.community_cycle(aid, fixture_rpc)
        with tx(aid) as c:
            failed = one(
                c, "SELECT state,result FROM lead.tasks WHERE id=%s", (pending["id"],)
            )
            assert failed["state"] == "failed" and failed["result"]["retry_after"]
            c.execute(
                "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
                (j({"retry_after": "2000-01-01T00:00:00+00:00"}), pending["id"]),
            )
        monkeypatch.setattr(model, "run_model", no_new_question)
        await community_runtime.community_cycle(aid, fixture_rpc)
        with tx(aid) as c:
            assert (
                one(c, "SELECT state FROM lead.tasks WHERE id=%s", (pending["id"],))[
                    "state"
                ]
                == "succeeded"
            )
            assert (
                one(
                    c,
                    "SELECT result->>'reply_state' AS reply FROM lead.tasks WHERE id=%s",
                    (task["id"],),
                )["reply"]
                == "confirmed"
            )
        assert await actor.page.evaluate("localStorage.getItem('join_count')") == "1"
        # Chat search is the left global-search surface with a native peer chip.
        # A second peer in that surface must never be read or clicked.
        await actor.page.set_content("""<style>[hidden]{display:none!important}</style>
        <div id="column-left"><div class="input-search" hidden><span class="selector-user" data-key="-777777">Readers</span><input class="input-search-input"></div>
        <div id="search-container" hidden><a data-mid="200" data-peer-id="-777777">EPUB question</a><a data-mid="999" data-peer-id="-888888">Other chat</a></div></div>
        <div class="chat active" data-type="chat"><div class="chat-info"><span class="peer-title" data-peer-id="-777777">Readers</span>4 members</div><button aria-label="Search">Search</button><div class="input-message-input" contenteditable="true" data-peer-id="-777777"></div><div id="history"></div></div>
        <script>{const q=s=>document.querySelector(s);q('button').onclick=()=>{q('.input-search').hidden=false;q('#search-container').hidden=false};q('a[data-peer-id="-777777"]').onclick=()=>q('#history').innerHTML='<div class="bubble" data-mid="200"><div class="message">Which EPUB tool keeps a glossary?</div></div>';window.addEventListener('hashchange',()=>{if(location.hash.includes('?message=200'))q('a[data-peer-id="-777777"]').click()});q('a[data-peer-id="-888888"]').onclick=()=>{throw Error('cross-chat read')};}</script>""")
        found, source = await community_runtime.search_history(
            actor.page, "EPUB", "-777777", 30
        )
        assert source == "native_group_search"
        assert [message["mid"] for message in found] == ["200"]
        await actor.page.locator(".selector-user").evaluate(
            "e=>e.dataset.key='-888888'"
        )
        found, source = await community_runtime.search_history(
            actor.page, "EPUB", "-777777", 30
        )
        assert found == [] and source == "native_group_search_scope_unverified"
        await actor.page.set_content("""<style>[hidden]{display:none!important}</style>
        <div class="chat active" data-type="chat"><div class="chat-info"><span class="peer-title" data-peer-id="-777777">Readers</span>4 members</div><button aria-label="Search">Search</button><div class="input-message-input" contenteditable="true" data-peer-id="-777777"></div>
        <div class="topbar-search-container" hidden><div class="topbar-search-input-container"><input class="topbar-search-input"><button class="input-search-clear">Clear</button></div><a data-mid="200" data-peer-id="-777777">EPUB</a><a data-mid="999" data-peer-id="-888888">Other chat</a></div><div id="history"></div></div>
        <script>{const q=s=>document.querySelector(s);q('button[aria-label=Search]').onclick=()=>q('.topbar-search-container').hidden=false;q('.input-search-clear').onclick=()=>q('.topbar-search-container').hidden=true;q('a[data-peer-id="-777777"]').onclick=()=>q('#history').innerHTML='<div class="bubble" data-mid="200"><div class="message">Which EPUB tool keeps a glossary?</div></div>';window.addEventListener('hashchange',()=>{if(location.hash.includes('?message=200'))q('a[data-peer-id="-777777"]').click()});}</script>""")
        found, source = await community_runtime.search_history(
            actor.page, "EPUB", "-777777", 30
        )
        assert source == "native_group_search"
        assert [message["mid"] for message in found] == ["200"]
        assert not await actor.page.locator(".topbar-search-input").is_visible()
        await actor.page.set_content("""<style>[hidden]{display:none!important}</style>
        <div class="chat active is-go-down-visible" data-type="chat"><div class="chat-info"><span class="peer-title" data-peer-id="-777777">Readers</span></div><button class="bubbles-go-down">Latest</button><div class="bubble" data-mid="1"><div class="message">Old search result</div></div></div>
        <div class="chat" data-type="chat" hidden><button class="bubbles-go-down" onclick="throw Error('wrong group')">Latest</button></div>
        <script>{const q=s=>document.querySelector(s);let n=0;q('.chat.active .bubbles-go-down').onclick=()=>{if(++n===2){q('.chat.active').classList.remove('is-go-down-visible');q('.bubble').dataset.mid='2';q('.message').textContent='Current reading question'}localStorage.setItem('latest_clicks',String(n))};}</script>""")
        await community_runtime.latest_position(actor.page, "-777777")
        assert (await community_runtime.read_history(actor.page, 30))[0]["mid"] == "2"
        assert await actor.page.evaluate("localStorage.getItem('latest_clicks')") == "2"
        # A stale left-column row does not switch the active chat. The product
        # must recover through the verified route and recheck the peer.
        await actor.page.set_content("""<div id="column-left"><div class="chatlist"><div class="chatlist-chat" data-peer-id="-777777">Readers</div></div></div>
        <div class="chat active" data-type="chat"><div class="chat-info"><span class="peer-title" data-peer-id="-888888">Previous chat</span></div></div>""")
        reopened = await community_runtime.observe_group(
            actor.page,
            "https://t.me/readers_fixture",
            identity,
            30,
            expected_peer="-777777",
        )
        assert reopened["target_id"] == "-777777"
        assert reopened["history"][0]["mid"] == "100"
        # A forum route opens a native topic drawer while the old group's chat
        # remains on screen. Only its bound peer's View as Messages may be used.
        await actor.page.set_content("""<style>[hidden]{display:none!important}</style>
        <div class="chat active" data-type="chat"><div class="chat-info"><span class="peer-title" data-peer-id="-888888">Previous group</span></div></div>
        <div class="topics-slider"><div class="topics-container active is-floating is-visible"><div class="sidebar-header"><span class="peer-title" data-peer-id="-777777">Readers</span><button class="sidebar-close-button">Close</button><button class="btn-menu-toggle">More</button></div></div></div>
        <div class="btn-menu-item" hidden><span class="tgico">message</span><span class="i18n">View as Messages</span></div>
        <script>{const q=s=>document.querySelector(s);q('.btn-menu-toggle').onclick=()=>q('.btn-menu-item').hidden=false;q('.sidebar-close-button').onclick=()=>q('.topics-container').classList.remove('is-visible');q('.btn-menu-item').onclick=()=>{q('.chat-info .peer-title').dataset.peerId='-777777';q('.topics-container').classList.remove('is-visible');q('.btn-menu-item').hidden=true;localStorage.setItem('forum_view','messages')};}</script>""")
        await telegram.open_forum_as_messages(actor.page, "-777777")
        assert (await actor.page.evaluate(telegram.TARGET))["target_id"] == "-777777"
        assert (
            await actor.page.evaluate("localStorage.getItem('forum_view')")
            == "messages"
        )
        await actor.page.locator(".topics-container").evaluate(
            "e=>e.classList.add('is-visible')"
        )
        await telegram.close_forum_topics(actor.page)
        assert not await actor.page.locator(".topics-container.is-visible").count()
        from lead_app.community_search import QUERIES, native_search

        with tx(aid) as c:
            original_task = one(
                c, "SELECT input FROM lead.tasks WHERE id=%s", (task["id"],)
            )
            search_input = {
                key: original_task["input"][key]
                for key in (
                    "account_id",
                    "external_account_id",
                    "delegation_id",
                    "control_version",
                    "config_revision",
                )
            }
            search_input["native_queries"] = [QUERIES[0]]
            search_task = one(
                c,
                "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,scheduled_at,workflow_id) VALUES(%s,%s,'community.discover','running',%s,now(),%s) RETURNING id",
                (current_tenant(), aid, j(search_input), "fixture:" + uid()),
            )
        await actor.page.set_content("""<style>[hidden]{display:none!important}</style>
        <div id="column-left"><div class="item-main"><div class="input-search"><input class="input-search-input" value="previous"><button class="input-search-clear">Clear</button></div><div class="chatlist">Chats</div><div id="search-container" hidden><div class="search-super"><div class="search-group-contacts"><a data-peer-id="-777777"><span class="peer-title">Tool Readers</span><span>@readers_fixture, 12 members</span></a></div></div></div></div></div>
        <script>{const q=s=>document.querySelector(s);q('input').onfocus=()=>{q('.item-main').classList.add('is-search-active');q('#search-container').hidden=false};q('button').onclick=()=>{q('input').value='';q('.item-main').classList.remove('is-search-active');q('#search-container').hidden=true};}</script>""")
        found = await native_search(actor, str(search_task["id"]), QUERIES[0])
        assert found["groups"][0]["telegram_url"] == "https://t.me/readers_fixture"
        assert await actor.page.locator(".item-main.is-search-active").count() == 1
        await actor.page.locator(".search-group-contacts").evaluate(
            'e=>e.innerHTML=\'<a data-peer-id="-888888"><span class="peer-title">Other group</span><span>@other_fixture, 2 members</span></a>\''
        )
        later = await native_search(actor, str(search_task["id"]), QUERIES[0])
        assert found["source"]["url"] == later["source"]["url"]
        # Public channels can rank before discussions. Apply the bounded
        # export after prioritizing member-based groups, without people.
        await actor.page.locator(".search-group-contacts").evaluate(
            """root=>{root.innerHTML=Array.from({length:15},(_,i)=>`<a data-peer-id="-${900000+i}"><span class="peer-title">Channel ${i}</span><span>@broadcast_fixture_${i}, 100 subscribers</span></a>`).join('')+'<a data-peer-id="-777777"><span class="peer-title">Translation Readers</span><span>@readers_fixture, 50 members</span></a><a data-peer-id="42"><span class="peer-title">Person</span><span>@person_fixture</span></a>'}"""
        )
        discussions = await native_search(actor, str(search_task["id"]), QUERIES[0])
        assert (
            discussions["groups"][0]["telegram_url"] == "https://t.me/readers_fixture"
        )
        assert len(discussions["groups"]) == 12
        assert all(int(group["peer_id"]) < 0 for group in discussions["groups"])
        with tx(aid) as c:
            candidates = community_growth.verified_candidates(
                c,
                community_growth.Discovery(
                    candidates=[
                        community_growth.Candidate(
                            name="Invented tool community",
                            telegram_url="https://t.me/readers_fixture",
                            source_url=found["source"]["url"],
                            reading_need="Reading fixture",
                            rules_summary="Unknown",
                        )
                    ],
                    summary="fixture",
                ),
                [found["source"], later["source"]],
            )
        assert candidates[0]["evidence_id"] == found["source"]["evidence_id"]
        assert candidates[0]["name"] == "Tool Readers"
        from lead_app.research import ResearchPage, begin_run, finish_run

        rid = begin_run(aid, "fixture search redirect", mode="community_discovery")
        research = ResearchPage(actor, rid)
        await research.ensure()
        destination_loaded = []

        async def search_redirect(route):
            from types import SimpleNamespace

            class RedirectRoute:
                request = route.request

                async def fetch(self, **options):
                    assert options["max_redirects"] == 0
                    return SimpleNamespace(
                        status=302, headers={"location": "https://t.me/readers_fixture"}
                    )

                async def fulfill(self, **options):
                    await route.fulfill(**options)

                async def abort(self):
                    await route.abort()

            await research.read_only_route(RedirectRoute())

        async def reject_destination(route):
            destination_loaded.append(route.request.url)
            await research.read_only_route(route)

        await research.context.route(
            "https://www.google.com/goto?url=fixture", search_redirect
        )
        await research.context.route("https://t.me/**", reject_destination)
        resolved = await research.command(
            "open", {"url": "https://www.google.com/goto?url=fixture"}, uid()
        )
        assert resolved["redirect_metadata_only"] and resolved["text"] == ""
        assert resolved["community_links"][0]["href"] == "https://t.me/readers_fixture"
        assert destination_loaded == []
        await research.context.route(
            "https://example.org/maintainer-fixture",
            lambda route: route.fulfill(
                body='<p>Telegram Group (Chinese): <a href="https://t.me/readers_fixture">Readers</a></p><p>Telegram Contact: <a href="https://t.me/person_fixture">Person</a></p>',
                content_type="text/html",
            ),
        )
        public = await research.command(
            "open", {"url": "https://example.org/maintainer-fixture"}, uid()
        )
        group = next(
            link
            for link in public["community_links"]
            if link["href"] == "https://t.me/readers_fixture"
        )
        contact = next(
            link
            for link in public["community_links"]
            if link["href"] == "https://t.me/person_fixture"
        )
        assert "Telegram Group (Chinese)" in group["context"]
        assert "Telegram Contact" in contact["context"]
        assert "Telegram Group" not in contact["context"]
        finish_run(aid, rid)
        await research.context.close()
        # Reassessment under a new policy inherits the confirmed membership
        # ledger. A group that removed membership must not be joined again.
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET input=input || %s WHERE id=%s",
                (j({"decision_version": "2"}), pending["id"]),
            )
            c.execute(
                "UPDATE lead.tasks SET state='succeeded',result=%s,completed_at=now() WHERE id=%s",
                (
                    j(
                        {
                            "candidates": [
                                {
                                    "name": "Readers",
                                    "telegram_url": "https://t.me/readers_fixture",
                                }
                            ]
                        }
                    ),
                    search_task["id"],
                ),
            )
        await actor.page.goto(
            "https://web.telegram.org/k/?account=1&fixture=lost#@readers_fixture"
        )
        with monkeypatch.context() as patch:
            patch.setattr(model, "run_model", relevant_community)
            await community_runtime.community_cycle(aid, product_rpc)
        with tx(aid) as c:
            lost = one(
                c,
                "SELECT input,result FROM lead.tasks WHERE type='community.engage' ORDER BY created_at DESC LIMIT 1",
            )
            assert lost["input"]["membership_task_id"] == str(task["id"])
            assert "失去成员身份" in lost["result"]["outcome"]["reason"]
        assert await actor.page.evaluate("localStorage.getItem('join_count')") == "1"
        # A swallowed native click leaves the entire draft in the client. Only
        # a later empty scoped search + unchanged server baseline permits one
        # continuation; an acknowledged/lost-receipt send above never retries.
        question = "How can I keep names stable in a new EPUB?"
        continuation_body = "For the new EPUB question, record names in one glossary before translating."
        await actor.page.goto(
            "https://web.telegram.org/k/?fixture=continuation#@readers_fixture"
        )
        await actor.page.evaluate(
            """question => {const q=s=>document.querySelector(s);q('.bubble').dataset.mid='200';q('.bubble .message').textContent=question;q('.reply-wrapper').textContent=question;q('#join').hidden=true;q('.input-message-input').hidden=false;
        q('.btn-send').onclick=()=>localStorage.setItem('swallowed_clicks',String(Number(localStorage.getItem('swallowed_clicks')||0)+1));
        q('.input-message-input').onkeydown=e=>{if(e.key!=='Enter')return;e.preventDefault();const body=e.currentTarget.textContent;const posts=JSON.parse(localStorage.getItem('posts')||'[]');posts.push(body);localStorage.setItem('posts',JSON.stringify(posts));const bubble=document.createElement('div');bubble.className='bubble is-out';bubble.dataset.mid='201';const text=document.createElement('div');text.className='message';text.textContent=body;bubble.append(text);q('#posts').append(bubble);e.currentTarget.textContent=''};}""",
            question,
        )
        with tx(aid) as c:
            original_input = one(
                c, "SELECT input FROM lead.tasks WHERE id=%s", (task["id"],)
            )["input"]
            continuation = one(
                c,
                "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,result,scheduled_at,workflow_id) VALUES(%s,%s,'community.engage','running',%s,%s,now(),%s) RETURNING id",
                (
                    current_tenant(),
                    aid,
                    j(
                        {
                            **original_input,
                            "reply": {
                                "body": continuation_body,
                                "reply_to_mid": "200",
                                "source_hash": community_runtime.digest(question),
                                "content_hash": community_runtime.digest(
                                    continuation_body
                                ),
                                "promote": False,
                            },
                        }
                    ),
                    j({"target_id": "-777777", "join_state": "confirmed"}),
                    "fixture:" + uid(),
                ),
            )
        assert (
            await community_runtime.browser_step(
                actor, str(continuation["id"]), "reply"
            )
        )["state"] == "unknown"
        with tx(aid) as c:
            c.execute(
                "INSERT INTO lead.activity(tenant_id,agent_id,title,detail,task_id,created_at) VALUES(%s,%s,'fixture','只核对已提交回复，不重复发送',%s,now()-interval '2 minutes')",
                (current_tenant(), aid, continuation["id"]),
            )

        async def no_existing_body(*args):
            return [], "no_matches"

        with monkeypatch.context() as patch:
            patch.setattr(community_runtime, "search_history", no_existing_body)
            assert (
                await community_runtime.browser_step(
                    actor, str(continuation["id"]), "reply"
                )
            )["state"] == "confirmed"
            assert (
                await community_runtime.browser_step(
                    actor, str(continuation["id"]), "reply"
                )
            )["state"] == "confirmed"
        assert (
            await actor.page.evaluate("localStorage.getItem('swallowed_clicks')") == "1"
        )
        assert json.loads(
            await actor.page.evaluate("localStorage.getItem('posts')")
        ) == [body, continuation_body]
        # Native approval requests are a distinct durable result, never a
        # confirmed membership or a reason to block the remaining research.
        await actor.page.goto(
            "https://web.telegram.org/k/?fixture=approval#@readers_fixture"
        )
        await actor.page.evaluate(
            """() => {const q=s=>document.querySelector(s);q('#join').remove();q('.input-message-input').hidden=false;const b=document.createElement('div');b.className='chat-input-control-button';b.textContent='APPLY TO JOIN GROUP';b.onclick=()=>{localStorage.setItem('apply_count',String(Number(localStorage.getItem('apply_count')||0)+1));b.textContent='CANCEL REQUEST';b.onclick=()=>{throw new Error('must not cancel')}};document.body.append(b)}"""
        )
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET state='succeeded',completed_at=now() WHERE state='running'"
            )
            approval = one(
                c,
                "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,result,scheduled_at,workflow_id) VALUES(%s,%s,'community.engage','running',%s,%s,now(),%s) RETURNING id",
                (
                    current_tenant(),
                    aid,
                    j(
                        {
                            **{
                                k: v
                                for k, v in original_input.items()
                                if k not in ("reply", "membership_task_id")
                            },
                            "join_approved": True,
                        }
                    ),
                    j({"target_id": "-777777"}),
                    "fixture:" + uid(),
                ),
            )
        preview = await community_runtime.browser_step(
            actor, str(approval["id"]), "observe"
        )
        assert not preview["joined"] and preview["apply_button_count"] == 1
        outcome = await community_runtime.browser_step(
            actor, str(approval["id"]), "join"
        )
        assert outcome["state"] == "pending_approval"
        assert (
            await community_runtime.browser_step(actor, str(approval["id"]), "join")
        )["state"] == "pending_approval"
        assert await actor.page.evaluate("localStorage.getItem('apply_count')") == "1"
        with tx(aid) as c:
            saved = one(
                c, "SELECT result FROM lead.tasks WHERE id=%s", (approval["id"],)
            )["result"]
            assert saved["join_submitted"] and saved["join_state"] == "pending_approval"
            c.execute(
                "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
                (j({"join_state": "unknown"}), approval["id"]),
            )
        reconciled = []

        async def reconcile_rpc(path, data):
            reconciled.append(data["operation"])
            if data["operation"] == "join":
                return {"state": "unknown", "reason": "fixture delayed acknowledgement"}
            return await community_runtime.browser_step(
                actor, data["task_id"], data["operation"]
            )

        assert (await community_runtime.community_cycle(aid, reconcile_rpc))[
            "delay"
        ] == 1
        assert reconciled == ["observe", "join"]
        with tx(aid) as c:
            result = one(
                c, "SELECT state,result FROM lead.tasks WHERE id=%s", (approval["id"],)
            )
            assert (
                result["state"] == "succeeded"
                and result["result"]["join_state"] == "unknown"
            )
            assert result["result"]["outcome"]["state"] == "pending_verification"
            assert result["result"]["join_reconciliation"]["controls"] == [
                "CANCEL REQUEST"
            ]
            c.execute(
                "UPDATE lead.tasks SET state='succeeded',completed_at=now(),result=result || %s WHERE id=%s",
                (j({"join_checked_at": "2000-01-01T00:00:00+00:00"}), approval["id"]),
            )

        async def confirmed_request_rpc(path, data):
            assert data["operation"] == "join"
            return await community_runtime.browser_step(
                actor, data["task_id"], data["operation"]
            )

        assert (await community_runtime.community_cycle(aid, confirmed_request_rpc))[
            "delay"
        ] == 1
        with tx(aid) as c:
            assert (
                one(
                    c,
                    "SELECT result->>'join_state' AS state FROM lead.tasks WHERE id=%s",
                    (approval["id"],),
                )["state"]
                == "pending_approval"
            )
            c.execute(
                "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
                (j({"join_checked_at": "2000-01-01T00:00:00+00:00"}), approval["id"]),
            )
        # A timer-driven read of the same request notices the later approval.
        await actor.page.evaluate(
            "document.querySelector('.chat-input-control-button').remove()"
        )

        async def approval_rpc(path, data):
            assert (
                data["task_id"] == str(approval["id"]) and data["operation"] == "join"
            )
            return await community_runtime.browser_step(
                actor, data["task_id"], data["operation"]
            )

        assert (await community_runtime.community_cycle(aid, approval_rpc))[
            "reason"
        ] == "community_joined"
        with tx(aid) as c:
            assert (
                one(
                    c,
                    "SELECT result->>'join_state' AS state FROM lead.tasks WHERE id=%s",
                    (approval["id"],),
                )["state"]
                == "confirmed"
            )
        assert await actor.page.evaluate("localStorage.getItem('apply_count')") == "1"
        # A click with no accepted native request may continue once only after
        # reloading the server-backed group state under the same identity.
        retry_html = html.replace(
            '<button id="join">Join Group</button>',
            '<div class="chat-input-control-button"><button id="join">Apply to Join Group</button></div>',
        )
        retry_html = retry_html.replace(
            "q('#join').hidden=true;q('.input-message-input').hidden=false;localStorage.setItem('join_count',String(Number(localStorage.getItem('join_count')||0)+1))",
            "localStorage.setItem('application_continuation_count',String(Number(localStorage.getItem('application_continuation_count')||0)+1));q('.chat-input-control-button').textContent='CANCEL REQUEST';q('.input-message-input').hidden=false",
        )
        await actor.page.route(
            "https://web.telegram.org/k/?fixture=join-continuation",
            lambda route: route.fulfill(body=retry_html, content_type="text/html"),
        )
        await actor.page.goto(
            "https://web.telegram.org/k/?fixture=join-continuation#@readers_fixture"
        )
        with tx(aid) as c:
            retry = one(
                c,
                "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,result,scheduled_at,workflow_id) VALUES(%s,%s,'community.engage','running',%s,%s,now(),%s) RETURNING id",
                (
                    current_tenant(),
                    aid,
                    j(
                        {
                            **{
                                k: v
                                for k, v in original_input.items()
                                if k not in ("reply", "membership_task_id")
                            },
                            "join_approved": True,
                        }
                    ),
                    j(
                        {
                            "target_id": "-777777",
                            "join_mode": "approval_request",
                            "join_submitted": True,
                            "join_state": "unknown",
                            "join_reconciliation": {
                                "at": "2000-01-01T00:00:00+00:00",
                                "joined": False,
                                "apply_button_count": 1,
                            },
                        }
                    ),
                    "fixture:" + uid(),
                ),
            )
        assert (await community_runtime.browser_step(actor, str(retry["id"]), "join"))[
            "state"
        ] == "pending_approval"
        assert (await community_runtime.browser_step(actor, str(retry["id"]), "join"))[
            "state"
        ] == "pending_approval"
        assert (
            await actor.page.evaluate(
                "localStorage.getItem('application_continuation_count')"
            )
            == "1"
        )
        with tx(aid) as c:
            assert (
                one(c, "SELECT result FROM lead.tasks WHERE id=%s", (retry["id"],))[
                    "result"
                ]["join_ui_continuation"]["server_state_refreshed"]
                is True
            )
            c.execute(
                "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
                (j({"join_state": "unknown"}), retry["id"]),
            )
        await actor.page.evaluate(
            "document.querySelector('.chat-input-control-button').textContent='APPLY TO JOIN GROUP'"
        )
        assert (await community_runtime.browser_step(actor, str(retry["id"]), "join"))[
            "state"
        ] == "unknown"
        assert (
            await actor.page.evaluate(
                "localStorage.getItem('application_continuation_count')"
            )
            == "1"
        )
        domain.set_control(aid, "pause")
        with pytest.raises(ValueError):
            await community_runtime.browser_step(actor, str(task["id"]), "reply")
    finally:
        await gateway.close(aid)
