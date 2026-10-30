import pytest
from lead_app import community_growth as growth
from lead_app import domain
from lead_app.db import one, tx
from pydantic import ValidationError


def test_strategy_persists_limits_without_granting_community_access(monkeypatch):
    monkeypatch.setattr(growth, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    assert growth.get_strategy(aid)["config"]["enabled"] is False
    result = growth.set_strategy(aid, {"enabled": True, "topics": ["EPUB 阅读"]})
    assert result["config"]["enabled"]
    assert result["config"]["max_promotions_per_week"] == 1
    assert not result["participation"]["proactive_AI_introduction"]
    assert result["participation"]["disclose_brand_when_promoting"]
    assert result["participation"]["truthful_identity_answers"]
    with tx(aid) as c:
        assert not one(c, "SELECT id FROM lead.authorization_grants")
    with pytest.raises(ValidationError):
        growth.GrowthConfig(enabled=True, max_replies_per_day=1000)
    with pytest.raises(ValidationError):
        growth.GrowthConfig(enabled=True, conceal_ai=True)


@pytest.mark.parametrize("action", ["join", "reply", "promote", "direct_message"])
def test_public_link_or_model_suggestion_never_grants_access(action):
    result = growth.participation_gate(
        {"telegram_url": "https://t.me/reading", "reading_need": "high"},
        action,
        relevant_question=True,
        promotion_requested=True,
    )
    assert not result["allowed"]


def test_context_and_access_are_separate_from_admin_permission():
    admin = {
        "kind": "discussion",
        "rules_evidence": "rules-page",
        "admin_automation_permission": True,
    }
    assert (
        growth.participation_gate(admin, "reply", relevant_question=True)["reason"]
        == "history_access_required"
    )
    opted_in = {**admin, "history_access_verified": True}
    assert (
        growth.participation_gate(opted_in, "reply")["reason"] == "no_relevant_question"
    )
    assert (
        growth.participation_gate(opted_in, "promote", relevant_question=True)["reason"]
        == "promotion_not_invited"
    )
    assert (
        growth.participation_gate(
            {**opted_in, "commercial_permission": True},
            "promote",
            relevant_question=True,
            promotion_requested=True,
        )["reason"]
        == "within_owner_strategy"
    )
    assert (
        growth.participation_gate({**opted_in, "reports": True}, "join")["reason"]
        == "participation_stopped"
    )


@pytest.mark.parametrize(
    "href,name,follow",
    [
        ("https://github.com/readest/readest/discussions", "Discussions", True),
        (
            "https://www.google.com/goto?url=opaque",
            "读书讨论 Telegram Messenger https://t.me",
            True,
        ),
        ("https://github.com/contact/report-content", "Report repository", False),
        ("https://www.google.com/search?udm=forums", "Forums", False),
        (
            "https://www.google.com/goto?url=opaque",
            "Translations https://translations.telegram.org",
            False,
        ),
    ],
)
def test_community_sources_skip_generic_navigation(href, name, follow):
    from lead_app.research import community_entry

    assert community_entry({"href": href, "name": name}) is follow


@pytest.mark.asyncio
async def test_discovery_network_blocks_telegram_and_message_aggregators():
    from lead_app.research import ResearchPage

    class Actor:
        aid = "fixture"

        async def route(self, route):
            route.passed = True

    class Route:
        def __init__(self, url):
            self.request = type("Request", (), {"url": url, "method": "GET"})()
            self.aborted = self.passed = False

        async def abort(self):
            self.aborted = True

    page = ResearchPage(Actor(), "fixture")
    page.community_discovery = True
    for url in [
        "https://t.me/reading",
        "https://web.telegram.org/k/",
        "https://tgstat.com/channel/example",
        "https://telemetr.io/en/channels/example",
    ]:
        route = Route(url)
        await page.read_only_route(route)
        assert route.aborted and not route.passed
    route = Route("https://example.org/community-rules")
    await page.read_only_route(route)
    assert route.passed and not route.aborted
    # An opaque Google result can be resolved without ever loading Telegram.
    from types import SimpleNamespace

    frame = object()
    page.page = SimpleNamespace(main_frame=frame, url="about:blank")
    page.redirect_source_url = "https://www.google.com/goto?url=opaque-result"
    route = Route("https://t.me/readers_fixture")
    route.request.is_navigation_request = lambda: True
    route.request.frame = frame
    await page.read_only_route(route)
    assert route.aborted and not route.passed
    page.redirect_metadata_only = True
    result = await page.observe()
    assert result["url"] == page.redirect_source_url
    assert result["community_links"][0]["href"] == "https://t.me/readers_fixture"
    assert result["text"] == "" and result["redirect_metadata_only"]

    async def headers_only(**options):
        assert options["max_redirects"] == 0
        return SimpleNamespace(
            status=302, headers={"location": "https://t.me/+fixture-invite"}
        )

    async def neutral_page(**options):
        assert options["status"] == 200

    route = Route(page.redirect_source_url)
    route.request.is_navigation_request = lambda: True
    route.fetch, route.fulfill = headers_only, neutral_page
    await page.read_only_route(route)
    result = await page.observe()
    assert result["redirect_observation"]["kind"] == "invite"
    assert not result["redirect_observation"]["destination_content_loaded"]
    assert result["community_links"] == [] and result["text"] == ""


def test_contextual_promotion_checks_affiliation_invitation_and_product_link():
    from lead_app.community_runtime import CommunityDecision, valid_reply

    original = {
        "own": False,
        "mid": "123",
        "body": "Which tool supports an EPUB glossary?",
    }
    business = {"product_url": "https://seka.one"}
    decision = CommunityDecision(
        relevant_question=True,
        commercial_allowed=True,
        promotion_invited=True,
        promote=True,
        reply_to_mid="123",
        body="我负责 Seka 的社群运营，可以从术语表开始。https://seka.one",
        reason="用户询问术语表工具，社群允许产品介绍。",
    )
    assert valid_reply(decision, original, business)
    for body in (
        "Sou da equipe Seka. Podemos ajudar com o glossário. https://seka.one",
        "Я из команды Seka. Начните с глоссария. https://seka.one",
    ):
        assert valid_reply(
            decision.model_copy(update={"body": body}), original, business
        )
    assert not valid_reply(
        decision.model_copy(
            update={"body": "Seka uses AI to translate EPUB files. https://seka.one"}
        ),
        original,
        business,
    )
    assert not valid_reply(
        decision.model_copy(update={"promotion_invited": False}), original, business
    )
    assert not valid_reply(
        decision.model_copy(update={"body": "我亲测很不错，立即购买"}),
        original,
        business,
    )
    assert not valid_reply(
        decision.model_copy(
            update={"body": "Seka AI 助手补充 https://elsewhere.example"}
        ),
        original,
        business,
    )
    assert not valid_reply(
        decision.model_copy(update={"promote": False}), original, business
    )
    assert not valid_reply(decision, {**original, "own": True}, business)
    ordinary = decision.model_copy(
        update={"body": "先固定术语表，再检查不同章节的同一术语。", "promote": False}
    )
    assert valid_reply(ordinary, original, business)
    assert not valid_reply(
        ordinary.model_copy(update={"body": "我是真人，不是机器人。"}),
        original,
        business,
    )
    assert not valid_reply(
        ordinary.model_copy(
            update={"body": "我是 Seka 的自动化运营账号。先固定术语表，再逐章核对。"}
        ),
        original,
        business,
    )
    assert not valid_reply(
        ordinary.model_copy(
            update={
                "body": "I'm Seka's AI community assistant here in the group. Start with a glossary."
            }
        ),
        original,
        business,
    )
    assert valid_reply(
        ordinary.model_copy(update={"body": "我是 Seka 的自动化运营账号。"}),
        {**original, "body": "你是 Seka 的自动化账号吗？"},
        business,
    )


def test_research_request_is_persisted_and_idempotent(monkeypatch):
    from lead_app.db import uid

    monkeypatch.setattr(growth, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    request = uid()
    with pytest.raises(ValueError, match="尚未启用"):
        growth.request_research(aid, request)
    growth.set_strategy(aid, {"enabled": True})
    assert growth.request_research(aid, request) == growth.request_research(
        aid, request
    )
    with tx(aid) as c:
        assert (
            one(c, "SELECT onboarding_state FROM lead.agents")["onboarding_state"][
                "community_research_request"
            ]
            == request
        )


@pytest.mark.asyncio
async def test_failed_discovery_has_a_short_retry_deadline(monkeypatch):
    from lead_app.db import j, uid
    from lead_app.scope import current_tenant

    monkeypatch.setattr(growth, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    growth.set_strategy(aid, {"enabled": True})
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,started_at) VALUES(%s,%s,'community.discover','failed',%s,'{}',%s,now(),now())",
            (
                current_tenant(),
                aid,
                "fixture-discovery:" + uid(),
                j({"error_type": "ModelTimeoutError"}),
            ),
        )
    result = await growth.discover_communities(aid)
    assert 1 <= result["delay"] <= 601
    assert result["reason"] == "community_discovery_retry_scheduled"
