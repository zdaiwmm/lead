"""Experiment attribution never treats missing data or repeated events as wins."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from lead_app import domain
from lead_app import operating_playbooks as pb
from lead_app.adapters.catalog import SPECS
from lead_app.api import app
from lead_app.db import j, one, tx, uid
from lead_app.scope import current_tenant
from lead_app.security import token


def stats_for(channel, outcomes=(0, 0, 0), posts=(3, 3, 3), sessions=100):
    return [
        {
            "plan_id": plan["id"],
            "posts": posts[n],
            "mature_posts": 3,
            "sessions": sessions,
            "outcomes": outcomes[n],
        }
        for n, plan in enumerate(channel["plans"])
    ]


def test_all_non_telegram_channels_have_three_actionable_plans_and_seka_drafts():
    book = pb.catalog()
    assert {c["platform"] for c in book["channels"]} == set(SPECS) - {"telegram"}
    all_ids = set()
    for channel in book["channels"]:
        assert len(channel["plans"]) >= 3
        for plan in channel["plans"]:
            assert plan["id"] not in all_ids
            all_ids.add(plan["id"])
            assert set(plan["formats"]) <= set(SPECS[channel["platform"]].formats)
            assert len(plan["workflow"]) >= 4 and len(plan["first_batch"]) >= 6
            assert plan["template"] and plan["experiment"] and plan["iterate"]
            example = pb.business_examples()["plans"][plan["id"]]
            assert len(example["first_batch"]) == 6 and example["sample"]
    assert all_ids == set(pb.business_examples()["plans"])


def test_missing_or_stale_measurement_never_picks_winner():
    channel = pb.channel_for("facebook")
    result = pb.recommend(channel, stats_for(channel, (90, 1, 0)), "not_ready")
    assert result["state"] == "measurement_required"
    assert result["preferred_plan_id"] is None
    assert all(s["conversion_rate"] is None for s in result["stats"])


def test_small_immature_and_similar_samples_keep_exploring():
    channel = pb.channel_for("facebook")
    for sample in (
        stats_for(channel, (9, 0, 0), sessions=10),
        stats_for(channel, (10, 11, 12)),
        stats_for(channel, (0, 0, 0)),
    ):
        assert pb.recommend(channel, sample, "connected")["preferred_plan_id"] is None
    sample = stats_for(channel, (90, 0, 0))
    sample[1]["mature_posts"] = 2
    assert pb.recommend(channel, sample, "connected")["state"] == "exploring"


def test_preference_preserves_every_fifth_publication_for_a_challenger():
    channel = pb.channel_for("facebook")
    result = pb.recommend(channel, stats_for(channel, (80, 2, 1)), "connected")
    assert result["state"] == "provisional_preference"
    assert result["preferred_plan_id"] == channel["plans"][0]["id"]
    # Nine confirmed posts: the tenth is assigned to a challenger.
    assert result["selected_plan"]["id"] != result["preferred_plan_id"]
    result = pb.recommend(
        channel, stats_for(channel, (80, 2, 1), posts=(4, 3, 3)), "connected"
    )
    assert result["selected_plan"]["id"] == result["preferred_plan_id"]


def test_formats_and_telegram_scope_do_not_expand_execution(monkeypatch):
    channel = pb.channel_for("facebook")
    result = pb.recommend(
        channel, stats_for(channel, (80, 0, 0)), "connected", ["text"]
    )
    assert result["preferred_plan_id"] is None  # Missing media plan cannot be compared.
    assert "text" in result["selected_plan"]["formats"]
    assert pb.recommend(channel, [], "connected", [])["selected_plan"] is None
    monkeypatch.setattr(
        pb, "evidence", lambda *a: pytest.fail("must not query Telegram")
    )
    assert (
        pb.planning_context(
            None, [{"platform": "telegram", "target_id": uid()}], {"facts": {}}, 1
        )
        == {}
    )


def test_business_example_is_host_scoped_and_does_not_mutate_catalog(monkeypatch):
    monkeypatch.setattr(pb, "evidence", lambda *a: ([], "not_ready"))
    target = {"platform": "facebook", "target_id": uid(), "available_formats": ["text"]}
    business = {"id": uid(), "facts": {}, "product_url": "https://seka.one"}
    context = pb.planning_context(None, [target], business, 1)[target["target_id"]]
    assert context["destination_url"] == "https://seka.one/translate"
    assert "EPUB" in context["selected_plan"]["first_batch"][0]
    assert "sample" not in pb.channel_for("facebook")["plans"][0]
    business["product_url"] = "https://seka.one.evil.example"
    assert (
        "destination_url"
        not in pb.planning_context(None, [target], business, 1)[target["target_id"]]
    )


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    domain.connect_lab(aid)
    with tx(aid) as c:
        business = one(
            c,
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url,facts) VALUES(%s,%s,1,'Seka fixture','https://seka.one','{}') RETURNING *",
            (current_tenant(), aid),
        )
        target = one(c, "SELECT id FROM lead.account_targets")["id"]
    return aid, business, target


def seed_post(c, aid, business, target, *, age=10, probe=False, revision=1):
    tenant = current_tenant()
    published = datetime.now(timezone.utc) - timedelta(days=age)
    plan_id = one(
        c,
        """INSERT INTO lead.plans(tenant_id,agent_id,revision,agent_revision_id,business_profile_id,body,state,valid_until)
           VALUES(%s,%s,%s,(SELECT id FROM lead.agent_revisions WHERE revision=1),%s,%s,'active',now()+interval '1 day') RETURNING id""",
        (
            tenant,
            aid,
            revision,
            business["id"],
            j(
                {
                    "operating_strategy": {
                        "catalog_version": pb.catalog()["version"],
                        "plan_id": "facebook.problem_guide",
                        "objective": "signup",
                    }
                }
            ),
        ),
    )["id"]
    content = one(
        c,
        "INSERT INTO lead.content_items(tenant_id,agent_id,plan_id,target_id,revision,body,source_refs,validation_result,content_hash) VALUES(%s,%s,%s,%s,1,'{}','[]','{}',%s) RETURNING id",
        (tenant, aid, plan_id, target, uid()),
    )["id"]
    task = one(
        c,
        "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'lab.publish','succeeded',%s,%s,now()) RETURNING id",
        (tenant, aid, uid(), j({"bootstrap_verification": probe})),
    )["id"]
    c.execute(
        """INSERT INTO lead.actions(tenant_id,agent_id,task_id,target_id,content_id,grant_id,operation_key,type,intent,expected_control_version,expected_config_revision,state,external_id,confirmed_at)
           VALUES(%s,%s,%s,%s,%s,(SELECT id FROM lead.authorization_grants LIMIT 1),%s,'publish','{}',1,1,'confirmed','fixture',%s)""",
        (tenant, aid, task, target, content, uid(), published),
    )
    link = one(
        c,
        "INSERT INTO lead.tracking_links(tenant_id,agent_id,content_id,platform,public_code,destination_url,allowed_host) VALUES(%s,%s,%s,'facebook',%s,'https://seka.one/translate','seka.one') RETURNING id",
        (tenant, aid, content, uid().replace("-", "")[:16]),
    )["id"]
    return link, published


def test_sql_excludes_probes_immature_posts_and_duplicate_conversion_events(agent):
    aid, business, target = agent
    with tx(aid) as c:
        link, published = seed_post(c, aid, business, target)
        seed_post(c, aid, business, target, age=1, revision=2)
        seed_post(c, aid, business, target, probe=True, revision=3)
        connection = one(
            c,
            "INSERT INTO lead.analytics_connections(tenant_id,agent_id,provider,site_host,secret_ref,state,created_at,last_received_at) VALUES(%s,%s,'lead_site','https://seka.one','fixture','verified',now()-interval '30 days',now()) RETURNING id",
            (current_tenant(), aid),
        )["id"]
        c.execute(
            "INSERT INTO lead.analytics_credentials(tenant_id,agent_id,connection_id,secret_sha256,state,created_at,last_received_at) VALUES(%s,%s,%s,%s,'active',now()-interval '30 days',now())",
            (current_tenant(), aid, connection, uid()),
        )
        for n, (trust, excluded, delay) in enumerate(
            [
                ("client_observed", None, 1),
                ("client_observed", None, 2),
                ("redirect_observed", None, 1),
                ("client_observed", "bot", 1),
                ("client_observed", None, 8 * 24),
            ]
        ):
            visit = one(
                c,
                "INSERT INTO lead.visit_sessions(tenant_id,agent_id,tracking_link_id,source,source_session_id,trust_level,qualified,exclusion_reason,rule_version,started_at,last_seen_at) VALUES(%s,%s,%s,'fixture',%s,%s,true,%s,'fixture',%s,%s) RETURNING id",
                (
                    current_tenant(),
                    aid,
                    link,
                    uid(),
                    trust,
                    excluded,
                    published + timedelta(hours=delay),
                    published + timedelta(hours=delay),
                ),
            )["id"]
            if n == 0:
                for event_type in ("signup", "signup", "trial"):
                    c.execute(
                        "INSERT INTO lead.conversion_events(tenant_id,agent_id,connection_id,visit_session_id,source_event_id,type,trust_level,rule_version,occurred_at) VALUES(%s,%s,%s,%s,%s,%s,'server_verified','fixture',%s)",
                        (
                            current_tenant(),
                            aid,
                            connection,
                            visit,
                            uid(),
                            event_type,
                            published + timedelta(hours=3),
                        ),
                    )
        stats, coverage = pb.evidence(
            c, target, business, 1, pb.channel_for("facebook"), "signup"
        )
        assert coverage == "connected"
        assert len(stats) == 1
        assert {
            key: stats[0][key]
            for key in ("posts", "mature_posts", "sessions", "outcomes")
        } == {"posts": 2, "mature_posts": 1, "sessions": 2, "outcomes": 1}
        assert (
            pb.evidence(c, uid(), business, 1, pb.channel_for("facebook"), "signup")[0]
            == []
        )
        assert (
            pb.evidence(c, target, business, 2, pb.channel_for("facebook"), "signup")[0]
            == []
        )
        assert (
            pb.evidence(c, target, business, 1, pb.channel_for("facebook"), "purchase")[
                0
            ]
            == []
        )
        c.execute(
            "UPDATE lead.analytics_credentials SET last_received_at=now()-interval '2 days'"
        )
        assert (
            pb.evidence(c, target, business, 1, pb.channel_for("facebook"), "signup")[1]
            == "not_ready"
        )


def test_read_only_endpoint_is_authenticated_and_agent_scoped(agent):
    aid, _, _ = agent
    client = TestClient(app)
    assert client.get(f"/api/agents/{aid}/operating-playbooks").status_code == 401
    client.cookies.set("lead_session", token("owner"))
    response = client.get(f"/api/agents/{aid}/operating-playbooks")
    assert response.status_code == 200
    assert len(response.json()["catalog"]["channels"]) == 10
    assert client.get(f"/api/agents/{uid()}/operating-playbooks").status_code == 404
    assert pb.get_playbooks(UUID(aid))["targets"] == {}  # lab is excluded.


@pytest.mark.asyncio
async def test_planner_receives_selected_playbook_and_preserves_decision_evidence(
    agent, monkeypatch
):
    from lead_app import activities
    from lead_app.model import ContentDecision

    aid, business, target = agent
    context = {
        "catalog_version": pb.catalog()["version"],
        "objective": "signup",
        "platform": "facebook",
        **pb.recommend(pb.channel_for("facebook"), [], "not_ready", ["text"]),
    }
    monkeypatch.setattr(
        activities, "planning_context", lambda *a: {str(target): context}
    )

    async def review(schema, payload, **kwargs):
        assert (
            payload["operating_playbooks"][str(target)]["selected_plan"]["id"]
            == "facebook.problem_guide"
        )
        assert "operating_playbooks" in kwargs["system"]
        return ContentDecision(
            action="review", target_id=str(target), rationale="样本不足，继续观察。"
        )

    monkeypatch.setattr(activities, "run_model", review)
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
    result = await activities.autonomous_cycle({"agent_id": aid, "cycle_id": uid()})
    assert result["action"] == "review"
    with tx(aid) as c:
        saved = one(c, "SELECT body FROM lead.plans")["body"]["operating_strategy"]
        assert saved["plan_id"] == "facebook.problem_guide"
        assert saved["coverage"] == "not_ready"
        assert saved["preferred_plan_id"] is None
        assert not one(c, "SELECT id FROM lead.actions")


def test_handbook_matches_runtime_catalog():
    from scripts.render_operating_playbooks import ROOT, render

    assert (ROOT / "docs/CHANNEL_OPERATING_PLAYBOOKS.md").read_text() == render()
    assert Path(ROOT / "docs/CHANNEL_OPERATING_PLAYBOOKS.md").stat().st_size > 30000
