"""The public platform, audience and model are fixtures; the delivery ledger is real."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from lead_app import activities, api, browser, channel_delivery, domain
from lead_app.browser import Gateway
from lead_app.config import database_kwargs
from lead_app.db import all_rows, one, tx, uid
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


def install_fixture_research(monkeypatch, actors):
    # Isolate search and social pages. The real gateway still renders the page,
    # captures DOM evidence, stores the browser step and validates the source link.
    from lead_app import operating_work, resource_pressure
    from lead_app.browser_tools import BrowserTools
    from lead_app.research import ResearchPage, begin_run, finish_run

    monkeypatch.setenv("LEAD_APPROVED_DISCOVERY_CHANNELS", "reddit,facebook")
    monkeypatch.setattr(resource_pressure, "sample", lambda: {"defer_optional": False})
    by_profile = {
        str(actor.profile["id"]): platform for platform, actor in actors.items()
    }
    demand = "A store manager needs a shift handoff worksheet. Operators may share a relevant worksheet here."

    async def offline_route(controller, route):
        platform = by_profile[str(controller.actor.profile["id"])]
        url = route.request.url
        if url.startswith("https://www.google.com/search?"):
            markup = f'<div id="search"><h3>Staff scheduling discussion</h3><a href="{TARGETS[platform]}">Shift worksheet resource directory</a></div>'
        else:
            assert url == TARGETS[platform]
            markup = f"<article>{demand}</article>"
        await route.fulfill(
            content_type="text/html", body="<title>Staff scheduling</title>" + markup
        )

    async def validate_fixture(url):
        assert url in TARGETS.values() or url.startswith(
            "https://www.google.com/search?"
        )

    for actor in actors.values():
        monkeypatch.setattr(actor.forward_proxy, "validate_url", validate_fixture)
    monkeypatch.setattr(ResearchPage, "read_only_route", offline_route)

    class FixtureTransport(BrowserTools):
        async def command(self, operation, args=None):
            if not self.run_id:
                self.run_id = begin_run(
                    self.aid, self.goal, task_id=self.task_id, mode=self.mode
                )
            return await browser.research_command(
                self.aid,
                browser.ResearchCommand(
                    run_id=self.run_id,
                    command_id=uid(),
                    operation=operation,
                    args=args or {},
                ),
            )

        async def finish(self, state="succeeded"):
            records = finish_run(self.aid, self.run_id, state)
            await browser.close_research(self.aid, self.run_id)
            return records

    async def fixture_model(schema, context, **kwargs):
        if schema is operating_work.ResearchBrief:
            return schema(
                audience="store managers",
                hypothesis="staff need a worksheet",
                queries=["shift handoff worksheet"],
            )
        page = context["untrusted_observations"][0]
        passage = next(p for p in page["passages"] if "worksheet" in p["text"])
        fields = {}
        if schema is operating_work.ChannelCheck:
            assert "may share" in passage["text"]
            fields = {
                "channel_fit": "viable",
                "next_direction": "respond on the verified target",
            }
        return schema(
            title="Shift handoff",
            summary=passage["text"],
            limitations="Fixture only",
            findings=[
                dict(
                    source_id=page["source_id"],
                    quote_id=passage["quote_id"],
                    observation=passage["text"],
                    next_step="Check sharing rules",
                )
            ],
            **fields,
        )

    monkeypatch.setattr(operating_work, "BrowserTools", FixtureTransport)
    monkeypatch.setattr(operating_work, "run_model", fixture_model)


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
        one(
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
        if path.endswith("/target/inspect"):
            return await browser.inspect_target(
                aid, browser.TelegramTargetRequest(**data)
            )
        assert path == f"/agents/{aid}/publish"
        return await browser.publish_action(aid, browser.PublishRequest(**data))

    async def binding_gateway(path, *, body):
        from types import SimpleNamespace

        routed = {"open": browser.open_browser, "inspect": browser.inspect_account}
        obs = await routed[path.rsplit("/", 1)[1]](aid, browser.OpenRequest(**body))
        return SimpleNamespace(json=lambda: obs)

    monkeypatch.setattr(api, "gateway", binding_gateway)
    monkeypatch.setattr(activities, "browser_rpc", offline_rpc)
    install_fixture_research(monkeypatch, actors)
    links = {}
    prepared = {}
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
            with psycopg.connect(**database_kwargs(True)) as c:
                c.execute(
                    """UPDATE lead.platform_capabilities SET permission_state='approved',technical_state='verified'
                    WHERE platform=%s AND action_type='publish'""",
                    (platform,),
                )
            opened = await api.connect(aid, api.Connection(platform=platform))
            assert opened["managed_target_start"]
            account_id = opened["account_id"]
            await api.verify_login(
                aid,
                api.Connection(
                    platform=platform, account_id=account_id, start_managed_targets=True
                ),
            )
            with tx(aid) as c:
                clock = one(
                    c,
                    "SELECT id,version FROM lead.tasks WHERE type='channel.clock' AND input->>'platform'=%s AND input->>'lane'='delivery'",
                    (platform,),
                )
            args = {
                "agent_id": aid,
                "task_id": str(clock["id"]),
                "generation": clock["version"],
                "tick": uid(),
            }
            from lead_app.channel_research import channel_research_tick

            with tx(aid) as c:
                research = one(
                    c,
                    """SELECT id,version FROM lead.tasks WHERE type='channel.clock'
                    AND input->>'platform'=%s AND input->>'lane'='research'""",
                    (platform,),
                )
            research_args = {
                **args,
                "task_id": str(research["id"]),
                "generation": research["version"],
            }
            with monkeypatch.context() as unavailable:
                unavailable.setenv("LEAD_APPROVED_DISCOVERY_CHANNELS", "")
                assert (await channel_research_tick(research_args))[
                    "reason"
                ] == "source_permission_required"
            first = await channel_delivery.channel_delivery_tick(
                {**args, "tick": uid()}
            )
            assert first["reason"] == "no_available_verified_target", first
            for expected in (
                "operating_work_completed",
                "public_observation_ready",
                "operating_work_completed",
            ):
                step = await channel_research_tick({**research_args, "tick": uid()})
                with tx(aid) as c:
                    assert step.get("reason") == expected, (
                        step,
                        all_rows(
                            c,
                            "SELECT type,state,result FROM lead.tasks WHERE type LIKE 'operating.%%'",
                        ),
                    )
            step = await channel_research_tick({**research_args, "tick": uid()})
            assert step["reason"] == "target_verification_queued", step
            with tx(aid) as c:
                target_id = one(
                    c,
                    "SELECT id FROM lead.account_targets WHERE account_id=%s",
                    (account_id,),
                )["id"]
                probe = one(
                    c,
                    "SELECT id FROM lead.tasks WHERE type='connection.verify_publish' AND input->>'target_id'=%s",
                    (str(target_id),),
                )
            receipt = await activities.execute_task(
                {"task_id": str(probe["id"]), "agent_id": aid}
            )
            assert receipt["state"] == "confirmed", receipt
            with tx(aid) as c:
                evidence = channel_delivery.opportunity(c, target_id)
                assert evidence and evidence["source_url"] == TARGETS[platform]
                assert "worksheet" in evidence["observation"]
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
            prepared[platform] = (site, task, account_id, target_id)

        from lead_app.adapters.web import WebAdapter

        submit = WebAdapter.submit
        barrier = asyncio.Barrier(2)

        async def submit_together(self, page, observation):
            # Both actors reach the final submit gate before either writes.
            await asyncio.wait_for(barrier.wait(), timeout=15)
            if self.platform == "facebook":
                async with page.expect_response(
                    lambda r: "/__fixture/publish" in r.url
                ):
                    await submit(self, page, observation)
                raise ConnectionError("fixture disconnect after accepted submit")
            return await submit(self, page, observation)

        def request(platform):
            return {"task_id": str(prepared[platform][1]["id"]), "agent_id": aid}

        with monkeypatch.context() as fault:
            fault.setattr(WebAdapter, "submit", submit_together)
            results = await asyncio.gather(
                *(activities.execute_task(request(p)) for p in platforms)
            )
        assert [r["state"] for r in results] == ["confirmed", "unknown"], results
        assert len(prepared["facebook"][0].posts) == 2
        assert (await activities.execute_task(request("facebook")))[
            "state"
        ] == "confirmed"
        for platform in platforms:
            actor = actors[platform]
            site, task, account_id, target_id = prepared[platform]
            assert (await activities.execute_task(request(platform)))[
                "state"
            ] == "succeeded"
            assert len(site.posts) == 2
            links[platform] = task["input"]["link"]
            assert links[platform] in site.posts[-1]["body"]
            with tx(aid) as c:
                assert channel_delivery.opportunity(c, target_id) is None
                link_row = one(
                    c,
                    "SELECT account_id,placement,placement_ref FROM lead.tracking_links WHERE content_id=%s",
                    (task["input"]["content_id"],),
                )
                assert link_row == {
                    "account_id": account_id,
                    "placement": "post",
                    "placement_ref": str(target_id),
                }
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
                len(
                    all_rows(
                        c,
                        "SELECT a.id FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id WHERE a.state='confirmed' AND t.type LIKE '%%.publish'",
                    )
                )
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


def test_distribution_placement_retries_and_source_integrity(pixel, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    aid, _, client, headers, _, _ = pixel
    monkeypatch.setenv("LEAD_CHANNEL_ATOM_AGENT_IDS", aid)
    path = f"/api/agents/{aid}/tracking-links"
    body = {
        "destination_url": "https://site.example/?version=1#try",
        "platform": "discord",
        "intent_id": uid(),
        "placement": "profile",
        "campaign": "cold-start",
    }

    def create(payload):
        result = client.post(path, headers=headers, json=payload)
        assert result.status_code == 200, result.text
        return result.json()

    with ThreadPoolExecutor(2) as pool:
        first, replay = pool.map(create, [body, body])
    assert first == replay and first["public_code"].startswith("dc_")
    assert create({**body, "platform": "reddit"})["public_code"].startswith("rd_")
    assert create({**body, "placement": "post"})["id"] != first["id"]
    assert (
        client.post(
            path,
            headers=headers,
            json={**body, "destination_url": "https://else.example/"},
        ).status_code
        >= 400
    )
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), tx(aid) as c:
        c.execute(
            "UPDATE lead.tracking_links SET platform='reddit' WHERE id=%s",
            (first["id"],),
        )


def test_node_schema_attestation_reads_actual_migration(site_database, monkeypatch):
    from scripts import home_live_maintenance as site

    with psycopg.connect(**database_kwargs(True)) as c:
        monkeypatch.setattr(
            site,
            "site_sql",
            lambda query: "t" if c.execute(query).fetchone()[0] else "f",
        )
        assert site.channel_schema_ready() == "ready"
        c.execute(
            "ALTER TABLE lead.tracking_links DISABLE TRIGGER keep_tracking_source"
        )
        with pytest.raises(ValueError, match="schema"):
            site.channel_schema_ready()
