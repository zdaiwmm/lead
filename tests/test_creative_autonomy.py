import fcntl
from datetime import datetime, timezone

import av
import psycopg
import pytest
from fastapi.testclient import TestClient
from lead_app import assets, domain
from lead_app import creative_pipeline as production
from lead_app.adapters.catalog import validate
from lead_app.api import app
from lead_app.config import database_kwargs
from lead_app.creative_schema import PROFILES, CreativeBrief, CreativeCard
from lead_app.creative_worker import render
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.operating_workspace import (
    prepare_all,
    prepare_example,
    prepare_workspace,
    weekly_review,
)
from lead_app.scope import current_tenant
from lead_app.security import token
from PIL import Image
from psycopg import sql

from scripts.migrate import upgrade


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url,facts) VALUES(%s,%s,1,%s,'https://seka.one',%s)",
            (
                current_tenant(),
                aid,
                "Seka provides long-form translation with glossary and style settings.",
                j({"name": "Seka"}),
            ),
        )
    return aid


def brief(kind="image"):
    return CreativeBrief(
        kind=kind,
        headline="Prepare a clear glossary",
        cards=[
            CreativeCard(
                heading="Prepare", body="List names and key terms you want to review."
            ),
            CreativeCard(
                heading="Configure",
                body="Check the target language, glossary and style.",
            ),
            CreativeCard(
                heading="Review", body="Start with a sample you are allowed to use."
            ),
        ],
        cta="Explore the current options",
    )


def test_channel_drafts_are_account_free_durable_and_versioned(agent):
    first = prepare_all(agent)
    second = prepare_all(agent)
    assert len(first) == 10
    assert all(len(p["plans"]) >= 3 for p in first)
    assert {p["id"] for p in first} == {p["id"] for p in second}
    assert all(p["duplicate"] for p in second)
    with tx(agent) as c:
        assert not all_rows(c, "SELECT id FROM lead.social_accounts")
        assert not all_rows(c, "SELECT id FROM lead.actions")
        assert (
            one(c, "SELECT count(*) AS n FROM lead.tasks WHERE type='channel.prepare'")[
                "n"
            ]
            == 10
        )
    with pytest.raises(ValueError):
        prepare_workspace(agent, "telegram")


@pytest.mark.asyncio
async def test_real_media_production_for_all_ten_channels_without_login(agent):
    for platform, supported in PROFILES.items():
        kind = "image" if "image" in supported else "video"
        result = await production.produce(agent, platform, brief(kind))
        assert result["quality"]["text_fits"]
        publication = next(a for a in result["assets"] if a["role"] == "publication")
        row, path = assets.resolve(agent, publication["id"], publication["sha256"])
        assert row["rights"]["ai_generated"] is True
        assert row["metadata"]["generated"]["source_basis"]["business_revision"] == 1
        assert row["metadata"]["generated"]["role"] == "publication"
        assert (
            validate(
                platform,
                {
                    "body": "A practical guide.",
                    "title": "Guide",
                    "made_for_kids": False,
                    "commercial_disclosure": "own_brand",
                },
                [row],
            )
            == kind
        )
        assert (row["metadata"]["width"], row["metadata"]["height"]) == supported[kind]
        if kind == "video":
            with av.open(str(path)) as container:
                assert next(container.decode(video=0)).width == supported[kind][0]
            assert "WEBVTT" in result["vtt"] and result["captions"]
            assert result["captions"][-1]["end"] == row["metadata"]["duration"]
        else:
            with Image.open(path) as image:
                image.load()
                assert image.size == supported[kind]
    with tx(agent) as c:
        assert not all_rows(c, "SELECT id FROM lead.social_accounts")
        assert not all_rows(c, "SELECT id FROM lead.actions")
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.tasks WHERE type='media.generate' AND state='succeeded'",
            )["n"]
            == 10
        )


@pytest.mark.asyncio
async def test_job_dedupes_and_source_changes_produce_new_assets(agent):
    first = await production.produce(agent, "facebook", brief())
    same = await production.produce(agent, "facebook", brief())
    assert same["duplicate"] and first["job_id"] == same["job_id"]
    with tx(agent) as c:
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url,facts) VALUES(%s,%s,2,'Updated business','https://seka.one',%s)",
            (current_tenant(), agent, j({"name": "Seka"})),
        )
    changed = await production.produce(agent, "facebook", brief())
    assert changed["job_id"] != first["job_id"]
    assert changed["source_basis"]["business_revision"] == 2
    # Tampered stored bytes cannot be returned as a successful cached render.
    stored = changed["assets"][0]
    _, path = assets.resolve(agent, stored["id"])
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="变化"):
        await production.produce(agent, "facebook", brief())


@pytest.mark.asyncio
async def test_interrupted_render_recovers_but_live_lock_keeps_the_job_reserved(agent):
    first = await production.produce(agent, "facebook", brief())
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.tasks SET state='running',started_at=now()-interval '10 minutes' WHERE id=%s",
            (first["job_id"],),
        )
    with production.lock_path(agent, first["job_id"]).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tx(agent) as c:
            production.recover_interrupted(c, agent)
            assert (
                one(c, "SELECT state FROM lead.tasks WHERE id=%s", (first["job_id"],))[
                    "state"
                ]
                == "running"
            )
    with tx(agent) as c:
        production.recover_interrupted(c, agent)
        assert (
            one(c, "SELECT state FROM lead.tasks WHERE id=%s", (first["job_id"],))[
                "state"
            ]
            == "failed"
        )
    recovered = await production.produce(agent, "facebook", brief())
    assert recovered["job_id"] == first["job_id"] and not recovered["duplicate"]


@pytest.mark.asyncio
async def test_pause_during_render_never_records_publishable_completion(
    agent, monkeypatch
):
    original = production.asyncio.create_subprocess_exec

    async def paused(*args, **kwargs):
        process = await original(*args, **kwargs)
        domain.set_control(agent, "pause")
        return process

    monkeypatch.setattr(production.asyncio, "create_subprocess_exec", paused)
    with pytest.raises(ValueError, match="状态"):
        await production.produce(agent, "facebook", brief())
    with tx(agent) as c:
        assert (
            one(c, "SELECT state FROM lead.tasks WHERE type='media.generate'")["state"]
            == "failed"
        )
        assert not all_rows(c, "SELECT id FROM lead.actions")


def test_renderer_rejects_text_that_would_be_clipped_and_no_media_is_silently_lost(
    tmp_path,
):
    too_long = brief().model_dump()
    too_long["cards"] = [{"heading": "Long", "body": "字" * 360}] * 6
    with pytest.raises(ValueError, match="OVERFLOW"):
        render(
            {
                "brief": too_long,
                "platform": "x",
                "brand": "Seka",
                "display_url": "seka.one",
            },
            tmp_path,
        )
    assert not (tmp_path / "poster.png").exists()


def test_weekly_review_keeps_unknowns_and_never_overwrites_user_preferences(agent):
    target = uid()
    contexts = {
        target: {
            "platform": "facebook",
            "catalog_version": "fixture",
            "objective": "signup",
            "state": "measurement_required",
            "coverage": "not_ready",
            "stats": [],
            "business_profile_id": uid(),
            "config_revision": 1,
        }
    }
    with tx(agent) as c:
        weekly_review(c, agent, contexts, datetime(2026, 11, 2, tzinfo=timezone.utc))
        weekly_review(c, agent, contexts, datetime(2026, 11, 3, tzinfo=timezone.utc))
        rows = all_rows(c, "SELECT kind,body,confirmation FROM lead.memory_entries")
        assert len(rows) == 1 and rows[0]["kind"] == "observation"
        assert rows[0]["body"]["preferred_plan_id"] is None
        contexts[target]["config_revision"] = 2
        weekly_review(c, agent, contexts, datetime(2026, 11, 3, tzinfo=timezone.utc))
        assert one(c, "SELECT count(*) AS n FROM lead.memory_entries")["n"] == 2


@pytest.mark.asyncio
async def test_prepared_example_and_http_assets_are_scoped_and_authenticated(agent):
    result = await prepare_example(agent, "tiktok")
    client = TestClient(app)
    assert client.get(f"/api/agents/{agent}/operations").status_code == 401
    client.cookies.set("lead_session", token("owner"))
    assert client.get(f"/api/agents/{agent}/operations").status_code == 200
    assert (
        client.get(
            f"/api/agents/{agent}/creative/{result['job_id']}/captions.vtt"
        ).status_code
        == 200
    )
    other = domain.create_agent()
    assert (
        client.get(
            f"/api/agents/{other}/creative/{result['job_id']}/captions.vtt"
        ).status_code
        == 404
    )
    with pytest.raises(LookupError):
        assets.resolve(other, result["assets"][0]["id"])


@pytest.mark.asyncio
async def test_profile_images_are_produced_without_enabling_photo_posts_on_tiktok(
    agent,
):
    from lead_app.operating_workspace import prepare_profile_assets

    result = await prepare_profile_assets(agent, "tiktok")
    assert set(result) == {"profile_avatar"}
    image = result["profile_avatar"]["assets"][0]
    row, _ = assets.resolve(agent, image["id"])
    assert row["metadata"]["width"] == row["metadata"]["height"] == 1024
    assert row["metadata"]["generated"]["purpose"] == "profile_avatar"
    with pytest.raises(ValueError):
        await production.produce(agent, "tiktok", brief("image"))


@pytest.fixture
def isolated_automation_database(monkeypatch):
    """Capability gates are altered only in a disposable test database."""
    name = "lead_creative_test_" + uid().replace("-", "")
    admin = database_kwargs(True)
    with psycopg.connect(**admin, autocommit=True) as c:
        c.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    try:
        upgrade({**admin, "dbname": name})
        monkeypatch.setenv("LEAD_DB_NAME", name)
        yield
    finally:
        monkeypatch.delenv("LEAD_DB_NAME", raising=False)
        with psycopg.connect(**admin, autocommit=True) as c:
            c.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.mark.asyncio
async def test_autonomous_cycle_creates_missing_media_then_queues_one_native_post(
    isolated_automation_database, monkeypatch
):
    from lead_app import activities
    from lead_app.adapters.catalog import SPECS
    from lead_app.model import ContentDecision

    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(activities, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url,facts) VALUES(%s,%s,1,'Seka provides long-form translation and glossary settings.','https://seka.one',%s)",
            (current_tenant(), aid, j({"name": "Seka"})),
        )
        profile_id = one(c, "SELECT id FROM lead.browser_profiles")["id"]
        account_id, target_id = uid(), uid()
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            "INSERT INTO lead.social_accounts(id,tenant_id,agent_id,profile_id,platform,external_account_id,display_name,state,automation_enabled) VALUES(%s,%s,%s,%s,'instagram','fixture','Fixture','connected',true)",
            (account_id, current_tenant(), aid, profile_id),
        )
        c.execute(
            "INSERT INTO lead.account_targets(id,tenant_id,agent_id,account_id,target_type,external_target_id,display_name,canonical_url,verified_permissions,state) VALUES(%s,%s,%s,%s,'profile','fixture','Fixture','https://www.instagram.com/fixture/',%s,'active')",
            (
                target_id,
                current_tenant(),
                aid,
                account_id,
                j(
                    {
                        "adapter_version": SPECS["instagram"].version,
                        "publishing_verified_formats": ["image"],
                    }
                ),
            ),
        )
        c.execute(
            "INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,granted_by) VALUES(%s,%s,%s,1,%s,'test')",
            (current_tenant(), aid, target_id, j(["publish"])),
        )
    with psycopg.connect(**database_kwargs(True)) as owner:
        owner.execute(
            "UPDATE lead.platform_capabilities SET technical_state='verified',permission_state='approved' WHERE platform='instagram' AND action_type='publish'"
        )

    async def author(schema, payload, **kwargs):
        if payload["targets"][0]["platform"] == "facebook":
            return ContentDecision(
                action="publish",
                target_id=str(payload["targets"][0]["target_id"]),
                body="Prepare your inputs and review the current Seka options.",
                rationale="Independent channel slot.",
            )
        assert not payload["assets"]
        assert payload["targets"][0]["platform"] == "instagram"
        return ContentDecision(
            action="publish",
            target_id=target_id,
            body="Start with a clear goal and a glossary. Review current options in Translate.",
            title="Prepare your first task",
            media_brief=brief(),
            rationale="Use the first guide topic.",
        )

    monkeypatch.setattr(activities, "run_model", author)
    cycle = uid()
    result = await activities.autonomous_cycle({"agent_id": aid, "cycle_id": cycle})
    assert "reason" not in result, result
    with tx(aid) as c:
        task = one(c, "SELECT * FROM lead.tasks WHERE type='instagram.publish'")
        assert task["state"] == "queued"
        assert task["input"]["media_job_id"]
        assert task["input"]["format"] == "image"
        assert task["input"]["calendar"]["slot"].startswith("launch:")
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.assets WHERE metadata->'generated'->>'purpose'='publication'",
            )["n"]
            == 1
        )
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.assets WHERE metadata->'generated'->>'purpose'='profile_avatar'",
            )["n"]
            == 1
        )
    action = activities.prepare_action(aid, str(task["id"]))
    assert action["state"] == "prepared"
    assert (await activities.autonomous_cycle({"agent_id": aid, "cycle_id": cycle}))[
        "duplicate"
    ]
    with tx(aid) as c:
        assert (
            one(
                c, "SELECT count(*) AS n FROM lead.tasks WHERE type='instagram.publish'"
            )["n"]
            == 1
        )
        c.execute(
            "UPDATE lead.actions SET state='dispatching' WHERE id=%s", (action["id"],)
        )
        c.execute(
            "UPDATE lead.actions SET state='confirmed',confirmed_at=now(),external_id='isolated-fixture' WHERE id=%s",
            (action["id"],),
        )
        c.execute("UPDATE lead.tasks SET state='succeeded' WHERE id=%s", (task["id"],))
        account2, target2 = uid(), uid()
        c.execute(
            "INSERT INTO lead.social_accounts(id,tenant_id,agent_id,profile_id,platform,external_account_id,state,automation_enabled) VALUES(%s,%s,%s,%s,'facebook','fixture2','connected',true)",
            (account2, current_tenant(), aid, profile_id),
        )
        c.execute(
            "INSERT INTO lead.account_targets(id,tenant_id,agent_id,account_id,target_type,external_target_id,display_name,canonical_url,verified_permissions,state) VALUES(%s,%s,%s,%s,'page','fixture2','Fixture 2','https://www.facebook.com/fixture2/',%s,'active')",
            (
                target2,
                current_tenant(),
                aid,
                account2,
                j(
                    {
                        "adapter_version": SPECS["facebook"].version,
                        "publishing_verified_formats": ["text"],
                    }
                ),
            ),
        )
        c.execute(
            "INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,granted_by) VALUES(%s,%s,%s,1,%s,'test')",
            (current_tenant(), aid, target2, j(["publish"])),
        )
    domain.connect_lab(aid)
    with tx(aid) as c:
        lab_target = one(
            c,
            "SELECT t.id FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE s.platform='lab'",
        )["id"]
        c.execute(
            "INSERT INTO lead.plans(tenant_id,agent_id,revision,agent_revision_id,business_profile_id,body,state,valid_until) SELECT %s,%s,coalesce(max(revision),0)+1,(SELECT id FROM lead.agent_revisions WHERE revision=1),(SELECT id FROM lead.business_profiles WHERE revision=1),%s,'active',now()+interval '1 day' FROM lead.plans",
            (
                current_tenant(),
                aid,
                j({"target_id": str(lab_target), "next_delay_seconds": 86400}),
            ),
        )
    with psycopg.connect(**database_kwargs(True)) as owner:
        owner.execute(
            "UPDATE lead.platform_capabilities SET technical_state='verified',permission_state='approved' WHERE platform='facebook' AND action_type='publish'"
        )
    again = await activities.autonomous_cycle({"agent_id": aid, "cycle_id": uid()})
    assert "reason" not in again, again
    with tx(aid) as c:
        assert (
            one(c, "SELECT state FROM lead.tasks WHERE type='facebook.publish'")[
                "state"
            ]
            == "queued"
        )
        assert not one(c, "SELECT id FROM lead.tasks WHERE type='lab.publish'")
