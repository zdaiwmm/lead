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
async def test_hybrid_discovery_uses_dom_evidence_instead_of_model_links(monkeypatch):
    from lead_app import hybrid_research, research

    aid = domain.create_agent()
    saved = {
        "phase": "hybrid_collect",
        "sources": [],
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
                        "text": "Reading discussions",
                        "community_links": [
                            {"name": "Readers", "href": "https://t.me/readers_group"}
                        ],
                    }
                ),
            ),
        )

    async def explored(*args, **kwargs):
        assert kwargs["mode"] == "community_discovery"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("selector_version,expected_cursor", [(7, 1), (8, 4)])
async def test_restricted_joined_candidate_continues_same_discovery(
    monkeypatch, selector_version, expected_cursor
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
    assert result["reason"] == "community_discovery_progress"
    assert calls[0][1]["task_id"] == str(task["id"])
    with tx(aid) as c:
        row = one(c, "SELECT state,result FROM lead.tasks WHERE id=%s", (task["id"],))
        assert row["state"] == "running"
        assert row["result"]["native_cursor"] == expected_cursor
        assert row["result"]["rejected_candidates"] == [candidate]
