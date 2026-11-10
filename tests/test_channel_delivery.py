"""The public platform, audience and model are fixtures; the delivery ledger is real."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from lead_app import activities, browser, channel_delivery, domain
from lead_app.adapters import get_adapter
from lead_app.adapters.catalog import SPECS
from lead_app.browser import Gateway
from lead_app.config import database_kwargs
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.first_entry import customers
from lead_app.migrations import upgrade
from lead_app.scope import current_tenant
from test_adapters import TARGETS, FixtureSite
from test_migrations import isolated_database as isolated_database
from test_pixel_events import pixel as pixel
from test_pixel_events import send


@pytest.fixture
def site_database(isolated_database, monkeypatch):
    from lead_app.db_pool import close_all

    upgrade(isolated_database)
    with monkeypatch.context() as environment:
        environment.setenv("LEAD_DB_NAME", isolated_database["dbname"])
        yield
        close_all()


@pytest.fixture(autouse=True)
def reclaim_test_profiles(site_database, reclaim_test_profiles):
    # Ensure the suite's profile finalizer runs before this disposable DB drops.
    yield


@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="requires native disposable browser",
)
async def test_channels_deliver_and_credit_only_first_arrival(pixel, monkeypatch):
    # These fixture-only platform permissions live in a freshly created database.
    aid, cid, client, _, _, server_key = pixel
    monkeypatch.setenv("LEAD_CHANNEL_ATOM_AGENT_IDS", aid)
    monkeypatch.setenv("LEAD_COLLECTOR_URL", "https://go.product.example")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "simulation-only")

    async def decision(model, context, **kwargs):
        target = context["targets"][0]
        evidence = context["operating_plan"]["channel_opportunity"]
        assert evidence["source_url"] == target["canonical_url"]
        return model(
            action="publish",
            title="Shift planning worksheet",
            body="Use our shift worksheet to confirm a last-minute change before sharing the updated schedule.",
            rationale=evidence["observation"],
            target_id=str(target["target_id"]),
        )

    monkeypatch.setattr(activities, "run_model", decision)
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        business = one(
            c,
            """INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url)
            VALUES(%s,%s,1,'Shift planning worksheet','https://site.example') RETURNING id""",
            (current_tenant(), aid),
        )
    gateway = Gateway()
    from lead_app.account_profiles import for_platform

    platforms = ("reddit", "facebook")
    profiles = {
        platform: for_platform(aid, platform, create=True) for platform in platforms
    }
    actors = dict(
        zip(
            platforms,
            await asyncio.gather(
                *(gateway.get(aid, p["id"]) for p in profiles.values())
            ),
        )
    )
    assert actors["reddit"].session != actors["facebook"].session
    assert actors["reddit"].folder != actors["facebook"].folder
    await actors["reddit"].context.add_cookies(
        [{"url": "https://site.example", "name": "slot_probe", "value": "reddit"}]
    )
    assert not any(
        cookie["name"] == "slot_probe"
        for cookie in await actors["facebook"].context.cookies()
    )

    monkeypatch.setattr(browser, "gateway", gateway)

    async def offline_rpc(path, data):
        assert path == f"/agents/{aid}/publish"
        return await browser.publish_action(aid, browser.PublishRequest(**data))

    monkeypatch.setattr(activities, "browser_rpc", offline_rpc)
    links = {}
    anonymous = uid()
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    try:
        for platform in platforms:
            actor = actors[platform]
            site = FixtureSite(platform)
            await actor.page.context.route("https://**/*", site.route)
            if platform == "facebook":
                await actor.page.context.add_cookies(
                    [
                        {
                            "name": "c_user",
                            "value": "10001",
                            "url": "https://www.facebook.com",
                        }
                    ]
                )
            await actor.page.goto(TARGETS[platform])
            adapter = get_adapter(platform)
            account = await adapter.account(actor.page)
            target = await adapter.target(
                actor.page, TARGETS[platform], account["external_account_id"]
            )
            with psycopg.connect(**database_kwargs(True)) as c:
                c.execute(
                    """UPDATE lead.platform_capabilities SET permission_state='approved',technical_state='verified'
                    WHERE platform=%s AND action_type='publish'""",
                    (platform,),
                )
            with tx(aid) as c:
                account_id = one(
                    c,
                    """INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,
                    external_account_id,state,automation_enabled) SELECT tenant_id,agent_id,id,%s,%s,'connected',true
                    FROM lead.browser_profiles WHERE id=%s RETURNING id""",
                    (
                        platform,
                        account["external_account_id"],
                        profiles[platform]["id"],
                    ),
                )["id"]
                target_id = one(
                    c,
                    """INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,
                    display_name,canonical_url,verified_permissions,state) VALUES(%s,%s,%s,%s,%s,'Fixture',%s,%s,'active') RETURNING id""",
                    (
                        current_tenant(),
                        aid,
                        account_id,
                        SPECS[platform].target_type,
                        target["external_target_id"],
                        TARGETS[platform],
                        j(
                            {
                                "actions": target["verified_permissions"],
                                "public_url": TARGETS[platform],
                                "publishing_verified_formats": ["text"],
                                "adapter_version": SPECS[platform].version,
                            }
                        ),
                    ),
                )["id"]
                c.execute(
                    """INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,granted_by)
                    VALUES(%s,%s,%s,1,'["publish"]','test_owner')""",
                    (current_tenant(), aid, target_id),
                )
                c.execute(
                    """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,completed_at)
                    VALUES(%s,%s,'operating.validate','succeeded',%s,%s,%s,now(),now())""",
                    (
                        current_tenant(),
                        aid,
                        uid(),
                        j(
                            {
                                "business_profile_id": str(business["id"]),
                                "config_revision": 1,
                            }
                        ),
                        j(
                            {
                                "status": "observed",
                                "channel_fit": "viable",
                                "source_urls": [TARGETS[platform]],
                                "findings": [
                                    {
                                        "observation": "A store manager is looking for a shift handoff worksheet.",
                                        "source_url": TARGETS[platform],
                                    }
                                ],
                            }
                        ),
                    ),
                )
            domain.channel_control(aid, platform, True)
            with tx(aid) as c:
                clock = one(
                    c,
                    "SELECT id,version FROM lead.tasks WHERE type='channel.clock' AND input->>'platform'=%s",
                    (platform,),
                )
            args = {
                "agent_id": aid,
                "task_id": str(clock["id"]),
                "generation": clock["version"],
                "tick": uid(),
            }
            await channel_delivery.channel_delivery_tick(args)
            with tx(aid) as c:
                task = one(
                    c,
                    "SELECT * FROM lead.tasks WHERE type=%s",
                    (platform + ".publish",),
                )
                assert task, one(
                    c, "SELECT result FROM lead.tasks WHERE id=%s", (clock["id"],)
                )
            params_task = {"task_id": str(task["id"]), "agent_id": aid}
            if platform == "facebook":
                from lead_app.adapters.web import WebAdapter

                submit = WebAdapter.submit

                async def lose_response(self, page, observation):
                    async with page.expect_response(
                        lambda r: "/__fixture/publish" in r.url
                    ):
                        await submit(self, page, observation)
                    raise ConnectionError("fixture disconnect after accepted submit")

                with monkeypatch.context() as fault:
                    fault.setattr(WebAdapter, "submit", lose_response)
                    uncertain = await activities.execute_task(params_task)
                assert uncertain["state"] == "unknown"
                assert len(site.posts) == 1
            result = await activities.execute_task(params_task)
            assert result["state"] == "confirmed", result
            assert (await activities.execute_task(params_task))["state"] == "succeeded"
            assert len(site.posts) == 1
            links[platform] = task["input"]["link"]
            assert links[platform] in site.posts[0]["body"]
            path = urlsplit(links[platform]).path
            redirect = client.get(
                path, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}
            )
            params = parse_qs(urlsplit(redirect.headers["location"]).query)
            entry = send(
                (aid, cid, client, None, None, None),
                anonymous_id=anonymous,
                session_id=uid(),
                ref=params["lr"][0],
                click_token=params["lc"][0],
            )
            assert entry.status_code == 200, entry.text
            await actor.page.context.unroute("https://**/*", site.route)
        with tx(aid) as c:
            assert (
                one(
                    c,
                    "SELECT count(*) AS n FROM lead.browser_sessions WHERE ended_at IS NULL",
                )["n"]
                == 2
            )
        await gateway.close_one(aid, str(profiles["facebook"]["id"]))
        with tx(aid) as c:
            assert (
                one(
                    c,
                    "SELECT state FROM lead.profile_leases WHERE profile_id=%s",
                    (profiles["reddit"]["id"],),
                )["state"]
                == "held"
            )
        event = {
            "connection_id": cid,
            "event_id": uid(),
            "event_name": "Purchase",
            "user_id": "new-store",
            "lead_attribution": entry.json()["attribution"],
            "properties": {"order_id": uid(), "value": 1, "currency": "CNY"},
        }
        for _ in range(2):
            response = client.post(
                "/collect/server/events",
                headers={"Authorization": "Bearer " + server_key},
                json=event,
            )
            assert response.status_code == 200, response.text
        with tx(aid) as c:
            assert customers(
                c,
                {"connection_id": cid, "name": "Purchase", "revision": 1},
                start,
                datetime.now(timezone.utc),
            ) == {"channels": {"reddit": 1}, "unknown": 0, "observed": 1}
            assert (
                len(all_rows(c, "SELECT id FROM lead.actions WHERE state='confirmed'"))
                == 2
            )
        # Snapshot and restore the complete account inventory with the project at rest.
        import json
        import secrets
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from lead_app import backups

        domain.set_control(aid, "pause")
        await gateway.close(aid)
        with TemporaryDirectory() as tmp:
            key = secrets.token_bytes(32)
            record = await asyncio.to_thread(
                backups._backup_profile, aid, Path(tmp), key
            )
            saved = await asyncio.to_thread(
                backups.restore_profile, Path(tmp) / (record["id"] + ".leadbackup"), key
            )
            assert set(
                json.loads((Path(saved["path"]) / "manifest.json").read_text())[
                    "account_profiles"
                ]
            ) == {str(p["id"]) for p in profiles.values()}
    finally:
        await gateway.close(aid)
