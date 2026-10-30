from types import SimpleNamespace

import pytest
from lead_app import community_discovery, community_growth, domain, model
from lead_app.community_links import telegram_link
from lead_app.db import j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://t.me/readers_group", "https://t.me/readers_group"),
        (
            "https://telegram.me/readers_group?utm_source=site",
            "https://t.me/readers_group",
        ),
        (
            "https://www.google.com/url?q=https%3A%2F%2Ft.me%2Freaders_group",
            "https://t.me/readers_group",
        ),
        ("https://t.me/+private-invite", None),
        (
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Ft.me%2Freaders_group",
            "https://t.me/readers_group",
        ),
        ("https://t.me/readers_group/123", None),
        ("https://t.me/share?url=https://example.org", None),
        ("https://t.me@elsewhere.example/readers_group", None),
    ],
)
def test_discovery_links_preserve_public_group_scope(value, expected):
    assert telegram_link(value) == expected


def test_repository_search_continues_only_actual_relevant_unvisited_links():
    source = "https://github.com/search?q=EPUB+translation+telegram&type=repositories"
    visited = "https://github.com/example/translate-book"
    pending = "https://github.com/example/epub-reader"
    links = [
        {"href": visited, "name": "example/translate-book"},
        {"href": pending, "name": "example/epub-reader"},
        {"href": "https://github.com/topics/epub", "name": "topics/epub"},
        {"href": "https://github.com/example/unrelated", "name": "example/unrelated"},
        {
            "href": "https://github.com/example/translate-book/issues",
            "name": "example/translate-book",
        },
        {"href": "https://elsewhere.org/example/epub", "name": "example/epub"},
        {"href": "https://github.com/example/epub?redirect=1", "name": "example/epub"},
        {"href": "https://github.com/example/epub", "name": "unrelated label"},
    ]
    saved = {
        "queue": ["https://example.org/visited", "https://example.org/pending"],
        "cursor": 1,
        "sources": [{"url": visited}],
    }
    assert community_discovery.queue_repository_entries(
        saved, {"url": source, "elements": links}
    ) == [pending]
    assert saved["queue"][0] == "https://example.org/visited"
    assert saved["queue"][1] == pending and saved["priority_web_until"] == 2
    assert (
        community_discovery.queue_repository_entries(
            saved, {"url": source, "elements": links}
        )
        == []
    )
    assert (
        community_discovery.queue_repository_entries(
            saved, {"url": "https://example.org/search", "elements": links}
        )
        == []
    )
    full = {"queue": [str(i) for i in range(48)], "cursor": 48, "sources": []}
    assert (
        community_discovery.queue_repository_entries(
            full, {"url": source, "elements": links}
        )
        == []
    )
    assert len(full["queue"]) == 48


@pytest.mark.parametrize(
    "subtitle,broadcast",
    [
        ("@tool, 1 subscriber", True),
        ("@tool, 100 subscribers", True),
        ("@tool, 100 members", False),
    ],
)
def test_native_metadata_distinguishes_channels_from_groups(subtitle, broadcast):
    assert community_discovery.broadcast_metadata({"subtitle": subtitle}) is broadcast


def test_discovery_visits_official_sources_before_expanding_queries(monkeypatch):
    monkeypatch.setattr(community_discovery, "hybrid_pending", lambda *a: False)
    payload = {"account_id": uid()}
    saved = {"native_cursor": 1, "cursor": 0, "queue": ["https://example.org"]}
    assert community_discovery.next_phase(payload, saved, ["reading"]) == "collect"
    saved["cursor"] = 1
    assert community_discovery.next_phase(payload, saved, ["reading"]) == "plan_queries"
    saved["query_plan_rounds"] = 3
    assert community_discovery.next_phase(payload, saved, ["reading"]) == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_short_model_request_has_no_hidden_transport_retry_and_closes(
    monkeypatch, fail
):
    from lead_app import admin_settings

    captured = {}
    settings = admin_settings.ModelSettings(default_model="fixture").model_dump()
    monkeypatch.setattr(
        admin_settings, "for_agent", lambda *a: (settings, "models/fixture@1")
    )
    monkeypatch.setattr(
        admin_settings,
        "content_context",
        lambda *a, **k: {"version": "brand/fixture@1", "documents": []},
    )

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def close(self):
            captured["closed"] = True

    class Agent:
        def __init__(self, *args, **kwargs):
            captured["settings"] = kwargs["model_settings"]

        async def run(self, *args, **kwargs):
            captured["run"] = kwargs
            if fail:
                raise TimeoutError("fixture timeout")
            return SimpleNamespace(
                output="ok",
                usage=SimpleNamespace(total_tokens=20, details={}, requests=1),
            )

    monkeypatch.setattr(
        model,
        "model_config",
        lambda: {
            "key": "fixture",
            "model": "fixture",
            "base_url": "https://provider.example",
        },
    )
    monkeypatch.setattr(model, "AsyncOpenAI", Client)
    monkeypatch.setattr(model, "OpenAIProvider", lambda **k: object())
    monkeypatch.setattr(model, "DeepSeekChatModel", lambda *a, **k: object())
    monkeypatch.setattr(model, "Agent", Agent)
    monkeypatch.setattr(model, "reserve", lambda *a: "reservation")
    monkeypatch.setattr(model, "settle", lambda *a, **k: captured.update(settlement=k))
    if fail:
        with pytest.raises(TimeoutError):
            await model.run_model(str, {}, agent_id="fixture", short_request=True)
        assert captured["settlement"]["known_unbilled"] is False
    else:
        assert (
            await model.run_model(str, {}, agent_id="fixture", short_request=True)
            == "ok"
        )
        assert captured["settlement"]["actual"] == 20
    assert captured["client"]["max_retries"] == 0
    assert captured["settings"]["timeout"] == 45
    assert captured["run"]["timeout"] == 100
    assert captured["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("same_adapter", [False, True])
async def test_model_timeout_keeps_discovered_links_and_same_checkpoint(
    monkeypatch, same_adapter
):
    monkeypatch.setattr(community_growth, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    community_growth.set_strategy(aid, {"enabled": True})
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        a = one(c, "SELECT * FROM lead.agents")
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs) VALUES(%s,%s,1,'EPUB reading','{}','[]')",
            (current_tenant(), aid),
        )
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,%s,'community.discover','running',%s,%s,%s,now()) RETURNING id",
            (
                current_tenant(),
                aid,
                "fixture:" + uid(),
                j(
                    {
                        "checkpoint_version": "2",
                        "control_version": a["control_version"],
                        "config_revision": a["config_revision"],
                    }
                ),
                j(
                    {
                        "phase": "assess",
                        "cursor": 0,
                        "queue": list(community_discovery.SEEDS),
                        "link_extractor_version": 5,
                        "sources": [],
                        "assessed_urls": [],
                        "errors": [],
                        "observed_candidates": [
                            {
                                "name": "Reading fixture",
                                "telegram_url": "https://t.me/readers_group",
                                "source_url": "https://example.org",
                                "page_excerpt": "Reading discussions",
                            }
                        ],
                    }
                ),
            ),
        )

    async def timeout(*a, **k):
        assert k["short_request"] is True
        raise TimeoutError("fixture")

    monkeypatch.setattr(model, "run_model", timeout)
    assert (await community_discovery.advance(aid))[
        "reason"
    ] == "community_discovery_progress"
    with tx(aid) as c:
        saved = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (task["id"],))
        assert saved["state"] == "running"
        assert saved["result"]["phase"] == "assess"
        assert saved["result"]["model_attempts"] == 1
        assert (
            saved["result"]["observed_candidates"][0]["telegram_url"]
            == "https://t.me/readers_group"
        )
        old_result = saved["result"]
        old_result["observed_candidates"][0]["page_excerpt"] = (
            "@readers_group, 1 subscriber"
        )
        old_result["adapter_revision"] = (
            community_discovery.ADAPTER_REVISION if same_adapter else 10
        )
        c.execute(
            "UPDATE lead.tasks SET state='failed',result=%s WHERE id=%s",
            (j(old_result), task["id"]),
        )
        if same_adapter:
            c.execute(
                "UPDATE lead.tasks SET completed_at=now()-interval '11 minutes' WHERE id=%s",
                (task["id"],),
            )

    async def unexpected_model(*a, **k):
        raise AssertionError(
            "Broadcast-only checkpoint must not request another model decision"
        )

    monkeypatch.setattr(model, "run_model", unexpected_model)
    await community_discovery.advance(aid)
    with tx(aid) as c:
        repaired = one(
            c, "SELECT state,result FROM lead.tasks WHERE id=%s", (task["id"],)
        )
        assert repaired["state"] == "running"
        assert repaired["result"]["phase"] == "collect"
        assert repaired["result"]["model_attempts"] == 0
        assert repaired["result"]["assessed_urls"] == ["https://t.me/readers_group"]
        assert one(c, "SELECT count(*) AS n FROM lead.tasks")["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [0, 2])
async def test_hybrid_discovery_uses_dom_evidence_instead_of_model_links(
    monkeypatch, attempts
):
    from lead_app import hybrid_research, research

    aid = domain.create_agent()
    saved = {
        "phase": "hybrid_collect",
        "sources": [{"url": "https://github.com/example/already-read"}],
        "hybrid_attempts": attempts,
        "observed_candidates": [],
        "assessed_urls": [],
        "errors": [],
    }
    with tx(aid) as c:
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,%s,'community.discover','running',%s,'{}',%s,now()) RETURNING *",
            (current_tenant(), aid, "fixture:" + uid(), j(saved)),
        )
    rid = research.begin_run(
        aid, "fixture public exploration", mode="community_discovery"
    )
    step = uid()
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.browser_steps(id,tenant_id,agent_id,run_id,seq,command_id,operation,input,state,output) VALUES(%s,%s,%s,%s,1,%s,'open','{}','completed',%s)",
            (
                step,
                current_tenant(),
                aid,
                rid,
                uid(),
                j(
                    {
                        "url": "https://example.org/readers",
                        "title": "Readers",
                        "text": "EPUB translation project\n"
                        + "Installation notes\n" * 400
                        + "\nTelegram Group (Chinese): Readers",
                        "community_links": [
                            {"name": "Readers", "href": "https://t.me/readers_group"}
                        ],
                    }
                ),
            ),
        )

    async def explored(*args, **kwargs):
        assert kwargs["mode"] == "community_discovery"
        assert args[1].startswith("https://github.com/search?")
        assert "README" in args[2]
        assert "https://github.com/example/already-read" in args[2]
        assert ("bilingual+ebook" in args[1]) == (attempts == 2)
        return {
            "run_id": rid,
            "sources": [
                {
                    "url": "https://example.org/readers",
                    "run_id": rid,
                    "evidence_id": step,
                }
            ],
            "text": "Invented alternative https://t.me/not_observed_group",
        }

    monkeypatch.setattr(hybrid_research, "explore", explored)
    await community_discovery.hybrid_collect(aid, task, saved)
    with tx(aid) as c:
        result = one(c, "SELECT result FROM lead.tasks WHERE id=%s", (task["id"],))[
            "result"
        ]
    assert result["phase"] == "assess"
    assert [x["telegram_url"] for x in result["observed_candidates"]] == [
        "https://t.me/readers_group"
    ]
    assert result["hybrid_run_id"] == rid
    assert (
        "Telegram Group (Chinese)" in result["observed_candidates"][0]["page_excerpt"]
    )
    assert len(result["observed_candidates"][0]["page_excerpt"]) <= 5000
    assert result["hybrid_attempts"] == attempts + 1
    assert result["hybrid_in_flight"] is False


def test_public_exploration_is_bounded_independently_of_adapter_revisions(monkeypatch):
    from lead_app import hybrid_research

    monkeypatch.setattr(hybrid_research, "available", lambda: True)
    for attempts in range(5):
        assert community_discovery.hybrid_pending(
            {"account_id": "fixture"},
            {
                "hybrid_attempts": attempts,
                "hybrid_adapter_revision": community_discovery.HYBRID_REVISION,
            },
        ) is (attempts < community_discovery.MAX_HYBRID_ATTEMPTS)
    assert not community_discovery.hybrid_pending({}, {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attempts,in_flight,outcome",
    [(3, False, "limit_reached"), (2, True, "interrupted")],
)
async def test_public_exploration_does_not_replay_interrupted_or_exhausted_attempt(
    monkeypatch, attempts, in_flight, outcome
):
    from lead_app import hybrid_research

    aid = domain.create_agent()
    saved = {
        "phase": "hybrid_collect",
        "hybrid_attempts": attempts,
        "hybrid_in_flight": in_flight,
    }
    with tx(aid) as c:
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,%s,'community.discover','running',%s,'{}',%s,now()) RETURNING *",
            (current_tenant(), aid, "fixture:" + uid(), j(saved)),
        )

    async def no_explore(*args, **kwargs):
        raise AssertionError("must not create another browser run")

    monkeypatch.setattr(hybrid_research, "explore", no_explore)
    await community_discovery.hybrid_collect(aid, task, saved)
    with tx(aid) as c:
        current = one(
            c, "SELECT state,result FROM lead.tasks WHERE id=%s", (task["id"],)
        )
        assert (
            current["state"] == "succeeded"
            and current["result"]["hybrid_attempts"] == attempts
        )
        assert current["result"]["hybrid_outcome"] == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("selector_version,expected_cursor", [(7, 1), (8, 4)])
@pytest.mark.parametrize("reply_state", [None, "confirmed", "unknown"])
async def test_restricted_joined_candidate_continues_same_discovery(
    monkeypatch, selector_version, expected_cursor, reply_state
):
    from lead_app import activities, channels
    from lead_app.community_growth import GrowthConfig

    monkeypatch.setattr(community_growth, "emit", lambda *a, **k: None)
    monkeypatch.setattr(channels, "emit", lambda *a, **k: None)
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
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs) VALUES(%s,%s,1,'EPUB reading','{}','[]')",
            (current_tenant(), aid),
        )
    channels.store_account(
        aid, "telegram", {"external_account_id": identity, "display_name": "Fixture"}
    )
    community_growth.set_strategy(aid, {"enabled": True, "mode": "contextual_outreach"})
    config = GrowthConfig(enabled=True, mode="contextual_outreach")
    candidate = {"telegram_url": "https://t.me/readers_fixture"}
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        a = one(c, "SELECT * FROM lead.agents")
        card = one(
            c, "SELECT id FROM lead.interaction_cards WHERE type='manage_community'"
        )
        payload = {
            **config.model_dump(),
            "checkpoint_version": "2",
            "control_version": a["control_version"],
            "config_revision": a["config_revision"],
            "account_id": str(account["id"]),
            "external_account_id": identity,
            "delegation_id": str(card["id"]),
        }
        saved = {
            "phase": "completed",
            "cursor": 0,
            "queue": list(community_discovery.SEEDS),
            "link_extractor_version": 5,
            "native_selector_version": selector_version,
            "native_cursor": 3,
            "candidates": [candidate],
            "observed_candidates": [],
            "assessed_urls": [],
            "sources": [],
            "errors": [],
        }
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,%s,'community.discover','succeeded',%s,%s,%s,now()) RETURNING id",
            (current_tenant(), aid, "fixture:" + uid(), j(payload), j(saved)),
        )
        c.execute(
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,%s,'community.engage','succeeded',%s,%s,%s,now())",
            (
                current_tenant(),
                aid,
                "fixture:" + uid(),
                j({"candidate": candidate, "decision_version": "2"}),
                j(
                    {
                        "join_state": "confirmed",
                        "join_submitted": True,
                        **({"reply_state": reply_state} if reply_state else {}),
                        "outcome": {
                            "state": "blocked",
                            "reason": "verification required",
                        },
                    }
                ),
            ),
        )
    calls = []

    async def search(path, data):
        calls.append((path, data))
        assert path.endswith("/community/search")
        return {
            "groups": [],
            "source": {
                "url": "https://web.telegram.org/k/",
                "run_id": uid(),
                "evidence_id": uid(),
                "native": True,
            },
        }

    monkeypatch.setattr(activities, "browser_rpc", search)
    result = await community_discovery.advance(aid)
    if reply_state == "unknown":
        assert result is None
        assert calls == []
        return
    assert result["reason"] == "community_discovery_progress"
    assert calls[0][1]["task_id"] == str(task["id"])
    with tx(aid) as c:
        row = one(c, "SELECT state,result FROM lead.tasks WHERE id=%s", (task["id"],))
        assert row["state"] == "running"
        assert row["result"]["native_cursor"] == expected_cursor
        assert row["result"]["rejected_candidates"] == [candidate]


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [0, community_discovery.MAX_SOURCES])
async def test_focused_queries_and_their_entry_links_keep_reserved_capacity(
    monkeypatch, cursor
):
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setattr(community_growth, "emit", lambda *a, **k: None)
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    community_growth.set_strategy(aid, {"enabled": True})
    config = community_growth.GrowthConfig(enabled=True)
    queue = [
        f"https://example.org/page/{i}" for i in range(community_discovery.MAX_SOURCES)
    ]
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        agent = one(c, "SELECT * FROM lead.agents")
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs) VALUES(%s,%s,1,'EPUB translation and terminology','{}','[]')",
            (current_tenant(), aid),
        )
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,%s,'community.discover','running',%s,%s,%s,now()) RETURNING id",
            (
                current_tenant(),
                aid,
                "fixture:" + uid(),
                j(
                    {
                        **config.model_dump(),
                        "checkpoint_version": "2",
                        "control_version": agent["control_version"],
                        "config_revision": agent["config_revision"],
                        "account_id": "fixture",
                        "native_queries": ["legacy_reader", "EPUB"],
                    }
                ),
                j(
                    {
                        "phase": "plan_queries",
                        "queue": queue,
                        "cursor": cursor,
                        "native_cursor": 0,
                        "native_selector_version": 8,
                        "link_extractor_version": 5,
                        "query_plan_rounds": 0,
                        "sources": [],
                        "observed_candidates": [],
                        "assessed_urls": [],
                        "errors": [],
                    }
                ),
            ),
        )

    async def plan(output, *args, **kwargs):
        assert output is community_discovery.QueryPlan
        return output(
            queries=["pdf2zh"],
            web_queries=[
                "site:t.me EPUB translation glossary",
                "PDF book translation Telegram",
                "OmegaT Telegram group",
            ],
            rationale="Seek explicit full-document translation needs.",
        )

    monkeypatch.setattr(model, "run_model", plan)
    await community_discovery.advance(aid)
    with tx(aid) as c:
        planned = one(
            c, "SELECT input,result FROM lead.tasks WHERE id=%s", (task["id"],)
        )
    saved = planned["result"]
    assert saved["phase"] == "collect"
    assert planned["input"]["native_queries"][:2] == ["EPUB", "pdf2zh"]
    assert saved["queue"][:cursor] == queue[:cursor]
    assert parse_qs(urlsplit(saved["queue"][cursor]).query)["q"] == [
        "site:t.me EPUB translation glossary"
    ]
    assert [urlsplit(url).hostname for url in saved["queue"][cursor : cursor + 3]] == [
        "www.google.com",
        "www.bing.com",
        "html.duckduckgo.com",
    ]
    assert len(saved["queue"]) == community_discovery.MAX_SOURCES + 3
    assert len(saved["queue"]) < community_discovery.source_limit(saved)

    class Browser:
        def __init__(self, *args, **kwargs):
            pass

        async def command(self, operation, args):
            assert operation == "open" and args["url"] == saved["queue"][cursor]
            return {
                "community_entries": [
                    {
                        "href": "https://example.org/translation-community",
                        "name": "EPUB translation discussion",
                    }
                ]
            }

        async def finish(self, *args):
            return []

    monkeypatch.setattr(community_discovery, "BrowserTools", Browser)
    await community_discovery.advance(aid)
    with tx(aid) as c:
        current = one(c, "SELECT result FROM lead.tasks WHERE id=%s", (task["id"],))[
            "result"
        ]
    assert current["cursor"] == cursor + 1
    assert current["queue"][cursor + 1] == "https://example.org/translation-community"
    assert current["phase"] == "collect"
    assert current["search_feedback"][-1]["entry_links"] == 1
    assert len(current["queue"]) <= community_discovery.source_limit(current)


@pytest.mark.parametrize(
    "native_cursor,rounds,web_cursor,priority,expected",
    [
        (1, 1, 0, 0, "native_collect"),
        (2, 1, 0, 0, "plan_queries"),
        (2, 1, 0, 1, "collect"),
        (2, 3, 0, 0, "native_collect"),
        (3, 3, 0, 0, "collect"),
        (3, 3, 1, 0, "completed"),
    ],
)
def test_focused_rounds_adapt_before_generic_fallback(
    native_cursor, rounds, web_cursor, priority, expected
):
    saved = {
        "native_cursor": native_cursor,
        "focused_native_until": 2,
        "query_plan_rounds": rounds,
        "cursor": web_cursor,
        "priority_web_until": priority,
        "queue": ["https://example.org"],
        "hybrid_adapter_revision": community_discovery.HYBRID_REVISION,
        "hybrid_attempts": community_discovery.MAX_HYBRID_ATTEMPTS,
    }
    assert (
        community_discovery.next_phase(
            {"account_id": "fixture", "plan_before_native": True},
            saved,
            ["focused1", "focused2", "generic"],
        )
        == expected
    )


@pytest.mark.parametrize("different_engines", [False, True])
def test_empty_public_engines_trigger_product_browser_use_before_generic_fallback(
    monkeypatch, different_engines
):
    monkeypatch.setattr(community_discovery, "hybrid_pending", lambda *args: True)
    saved = {
        "native_cursor": 8,
        "focused_native_until": 8,
        "query_plan_rounds": 2,
        "cursor": 3,
        "queue": ["https://example.org"] * 6,
        "search_feedback": [
            {
                "kind": "public",
                "url": "https://www.google.com/search?q=fixture",
                "telegram_links": 0,
                "entry_links": 0,
            },
            {
                "kind": "public",
                "url": (
                    "https://www.bing.com/search?q=fixture"
                    if different_engines
                    else "https://www.google.com/search?q=other"
                ),
                "telegram_links": 0,
                "entry_links": 0,
            },
        ],
    }
    assert community_discovery.next_phase(
        {"account_id": "fixture", "plan_before_native": True},
        saved,
        ["focused"] * 8 + ["generic"],
    ) == ("hybrid_collect" if different_engines else "plan_queries")


@pytest.mark.parametrize(
    "topics,expected",
    [
        (["EPUB 全文翻译", "双语阅读与术语一致性"], ["翻译", "双语", "EPUB", "术语"]),
        (["电子书阅读"], []),
    ],
)
def test_native_demand_queries_only_come_from_owner_topics(topics, expected):
    assert community_discovery.demand_queries(topics) == expected


@pytest.mark.asyncio
async def test_existing_checkpoint_recovers_footer_group_context_without_new_browsing(
    monkeypatch,
):
    from lead_app import research

    monkeypatch.setattr(community_growth, "emit", lambda *a, **k: None)
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    community_growth.set_strategy(aid, {"enabled": True})
    url = "https://example.org/ebook-translation"
    group = "https://t.me/readers_group"
    text = (
        "Full EPUB translation support.\n"
        + "Setup information\n" * 400
        + "\nContact\nTelegram Group (Chinese): @readers_group"
    )
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        agent = one(c, "SELECT * FROM lead.agents")
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs) VALUES(%s,%s,1,'EPUB full-book translation','{}','[]')",
            (current_tenant(), aid),
        )
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,%s,'community.discover','running',%s,%s,'{}',now()) RETURNING id",
            (
                current_tenant(),
                aid,
                "fixture:" + uid(),
                j(
                    {
                        "checkpoint_version": "2",
                        "control_version": agent["control_version"],
                        "config_revision": agent["config_revision"],
                    }
                ),
            ),
        )
    run = research.begin_run(
        aid,
        "fixture public footer",
        task_id=str(task["id"]),
        mode="community_discovery",
    )
    step = uid()
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.browser_steps(id,tenant_id,agent_id,run_id,seq,command_id,operation,input,state,output) VALUES(%s,%s,%s,%s,1,%s,'open','{}','completed',%s)",
            (
                step,
                current_tenant(),
                aid,
                run,
                uid(),
                j(
                    {
                        "url": url,
                        "title": "EPUB translation",
                        "text": text,
                        "community_links": [{"name": "@readers_group", "href": group}],
                    }
                ),
            ),
        )
        saved = {
            "phase": "collect",
            "queue": [url],
            "cursor": 0,
            "link_extractor_version": 5,
            "sources": [{"url": url, "run_id": run, "evidence_id": step}],
            "observed_candidates": [
                {
                    "name": "@readers_group",
                    "telegram_url": group,
                    "source_url": url,
                    "page_excerpt": text[:5000],
                }
            ],
            "assessed_urls": [group],
            "errors": [],
        }
        c.execute("UPDATE lead.tasks SET result=%s WHERE id=%s", (j(saved), task["id"]))

    async def assess(output, prompt, **kwargs):
        assert output is community_growth.Discovery
        assert "Telegram Group (Chinese)" in prompt["observed_links"][0]["page_excerpt"]
        return output(
            candidates=[
                community_growth.Candidate(
                    name="Readers",
                    telegram_url=group,
                    source_url=url,
                    reading_need="Full EPUB translation workflow",
                    rules_summary="Unknown",
                )
            ],
            summary="Observed maintainer group entry",
        )

    class UnexpectedBrowser:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Existing source evidence must be reused")

    monkeypatch.setattr(model, "run_model", assess)
    monkeypatch.setattr(community_discovery, "BrowserTools", UnexpectedBrowser)
    assert (await community_discovery.advance(aid))[
        "reason"
    ] == "community_candidates_ready"
    with tx(aid) as c:
        row = one(c, "SELECT state,result FROM lead.tasks WHERE id=%s", (task["id"],))
        assert row["state"] == "succeeded"
        assert row["result"]["link_context_version"] == 2
        assert row["result"]["candidates"][0]["telegram_url"] == group
        assert one(c, "SELECT count(*) AS n FROM lead.tasks")["n"] == 1
        assert one(c, "SELECT count(*) AS n FROM lead.browser_steps")["n"] == 1
