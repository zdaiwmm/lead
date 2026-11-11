"""Public distribution requires native visibility evidence, not a new task ID."""

import os
from datetime import datetime, timedelta, timezone

import pytest
from lead_app import domain, operating_plans
from lead_app import publication_delivery as delivery
from lead_app.db import j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.fixture
def pilot_target(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    account = uid()
    target = uid()
    grant = uid()
    with tx(aid) as c:
        profile = one(c, "SELECT id FROM lead.browser_profiles")["id"]
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            "INSERT INTO lead.social_accounts(id,tenant_id,agent_id,profile_id,platform,state,external_account_id,automation_enabled) VALUES(%s,%s,%s,%s,'tiktok','connected','pilot_owner',true)",
            (account, current_tenant(), aid, profile),
        )
        c.execute(
            "INSERT INTO lead.account_targets(id,tenant_id,agent_id,account_id,target_type,external_target_id,canonical_url,display_name,verified_permissions,state) VALUES(%s,%s,%s,%s,'profile','pilot_owner','https://www.tiktok.com/@pilot_owner/','Pilot owner',%s,'active')",
            (
                target,
                current_tenant(),
                aid,
                account,
                j({"adapter_version": "web-2", "actions": ["publish", "upload_media"]}),
            ),
        )
        c.execute(
            "INSERT INTO lead.authorization_grants(id,tenant_id,agent_id,target_id,version,allowed_actions,granted_by) VALUES(%s,%s,%s,%s,1,%s,'fixture-owner')",
            (grant, current_tenant(), aid, target, j(["publish", "upload_media"])),
        )

    return aid, account, target, grant


@pytest.fixture
def submitted(pilot_target, monkeypatch):
    aid, _, target, grant = pilot_target
    task, action = uid(), uid()
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.tasks(id,tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,completed_at) VALUES(%s,%s,%s,'tiktok.publish','succeeded',%s,%s,'{}',now(),now())",
            (
                task,
                current_tenant(),
                aid,
                "fixture:" + task,
                j(
                    {
                        "platform": "tiktok",
                        "target_id": target,
                        "title": "Owned caption",
                    }
                ),
            ),
        )
        c.execute(
            "INSERT INTO lead.actions(id,tenant_id,agent_id,task_id,target_id,grant_id,operation_key,type,intent,expected_control_version,expected_config_revision,state,external_id,external_url,confirmed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'publish',%s,1,1,'confirmed','fixture-ack','https://www.tiktok.com/@pilot_owner/video/12345',now())",
            (
                action,
                current_tenant(),
                aid,
                task,
                target,
                grant,
                "fixture:" + action,
                j(
                    {
                        "platform": "tiktok",
                        "format": "video",
                        "body": "Owned caption fixture-marker",
                        "marker": "fixture-marker",
                    }
                ),
            ),
        )
        c.execute(
            "UPDATE lead.account_targets SET verified_permissions=verified_permissions || %s WHERE id=%s",
            (
                j(
                    {
                        "publishing_verified_formats": ["video"],
                        "publishing_verified": True,
                    }
                ),
                target,
            ),
        )
    wakes = []
    monkeypatch.setattr(delivery, "emit", lambda *a: wakes.append(a))
    return aid, target, grant, task, action, wakes


def test_receipt_pending_review_is_not_a_public_business_outcome(submitted):
    aid, target, grant, task, action, wakes = submitted
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with tx(aid) as c:
        observed = delivery.persist(
            c, aid, action, {"state": "under_review", "visibility": "private"}, now=now
        )
        assert not observed["publicly_visible"]
        p = one(
            c,
            "SELECT verified_permissions p FROM lead.account_targets WHERE id=%s",
            (target,),
        )["p"]
        assert p["publishing_verified_formats"] == ["video"]
        assert "pending_publishing_formats" not in p
        assert domain.metrics(c)["published"] == 0
        agent = one(c, "SELECT * FROM lead.agents")
        item = next(
            x
            for x in operating_plans.business_tasks(
                c, agent, {"id": uid()}, day, day + timedelta(days=1)
            )
            if x["id"] == task
        )
        assert item["state"] == "waiting" and not item["completed_today"]
        assert "审核" in item["reason"] and item["receipt"] == "fixture-ack"
        delivery.persist(
            c,
            aid,
            action,
            {"state": "public", "visibility": "public"},
            now=now + timedelta(minutes=5),
        )
        assert domain.metrics(c)["published"] == 1
        p = one(
            c,
            "SELECT verified_permissions p FROM lead.account_targets WHERE id=%s",
            (target,),
        )["p"]
        assert (
            p["publishing_verified_formats"] == ["video"]
            and "pending_publishing_formats" not in p
        )
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"]
            == "running"
        )


def test_unchanged_review_does_not_change_capability_fingerprint_or_wake(submitted):
    aid, target, _, _, action, wakes = submitted
    now = datetime.now(timezone.utc)
    with tx(aid) as c:
        first = delivery.persist(
            c, aid, action, {"state": "under_review", "visibility": "private"}, now=now
        )
        before = one(
            c,
            "SELECT verified_permissions p FROM lead.account_targets WHERE id=%s",
            (target,),
        )["p"]
        second = delivery.persist(
            c,
            aid,
            action,
            {"state": "under_review", "visibility": "private"},
            now=now + timedelta(minutes=5),
        )
        after = one(
            c,
            "SELECT verified_permissions p FROM lead.account_targets WHERE id=%s",
            (target,),
        )["p"]
        assert (
            first["checked_at"] != second["checked_at"]
            and before == after
            and len(wakes) == 1
        )


def test_pause_and_revocation_are_never_undone_by_a_public_receipt(submitted):
    aid, target, grant, _, action, _ = submitted
    with tx(aid) as c:
        delivery.persist(
            c, aid, action, {"state": "under_review", "visibility": "private"}
        )
        c.execute("UPDATE lead.agents SET desired_state='paused'")
        c.execute(
            "UPDATE lead.authorization_grants SET revoked_at=now() WHERE id=%s",
            (grant,),
        )
        delivery.persist(c, aid, action, {"state": "public", "visibility": "public"})
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"] == "paused"
        )
        assert (
            one(
                c,
                "SELECT verified_permissions p FROM lead.account_targets WHERE id=%s",
                (target,),
            )["p"]["publishing_verified_formats"]
            == ["video"]
        )
        assert one(c, "SELECT revoked_at FROM lead.authorization_grants WHERE id=%s", (grant,))["revoked_at"]
        assert one(
            c, "SELECT revoked_at FROM lead.authorization_grants WHERE id=%s", (grant,)
        )["revoked_at"]


@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1", reason="native fixed website contracts"
)
@pytest.mark.asyncio
async def test_native_delivery_parser_scopes_visibility_to_the_known_video():
    from camoufox.async_api import AsyncCamoufox
    from lead_app.adapters.web import WebAdapter

    async with AsyncCamoufox(headless=True) as browser:
        page = await browser.new_page()
        adapter = WebAdapter("tiktok")
        url = "https://www.tiktok.com/@pilot_owner/video/12345"
        for labels, state in [
            ("Content under review</span><span>Only me", "under_review"),
            ("Only me", "private"),
            ("Everyone", "public"),
            ("Friends", "restricted"),
            ("Removed", "rejected"),
            ("No visibility evidence", "unknown"),
        ]:
            await page.set_content(
                '<table><tr><td>Owned caption fixture-marker</td><td><a href="'
                + url
                + '">Open</a></td><td><span>'
                + labels
                + '</span></td></tr><tr><td>Other caption other-marker</td><td><a href="https://www.tiktok.com/@pilot_owner/video/99999">Open</a></td><td><span>Everyone</span></td></tr></table>'
            )
            result = await adapter.delivery_status(
                page,
                url,
                {"body": "Owned caption fixture-marker", "marker": "fixture-marker"},
            )
            assert result["state"] == state
        assert not delivery.is_public("tiktok", None)
        assert delivery.is_public("telegram", None)


@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1", reason="native fixed website contracts"
)
@pytest.mark.asyncio
async def test_native_review_preserves_original_draft_and_cannot_publish():
    from camoufox.async_api import AsyncCamoufox
    from lead_app.adapters.web import WebAdapter

    async with AsyncCamoufox(headless=True) as browser:
        context = await browser.new_context()
        requests = []
        url = "https://www.tiktok.com/@pilot_owner/video/12345"

        async def route(r):
            requests.append((r.request.method, r.request.url))
            if "/tiktokstudio/content" in r.request.url:
                body = (
                    '<a data-e2e="nav-profile" href="/@pilot_owner/">Profile</a><input placeholder="Search for post description"><table><tr><td>Owned caption fixture-marker</td><td><a href="'
                    + url
                    + '">Open</a></td><td><span>Content under review</span><span>Only me</span></td></tr></table>'
                )
            elif "/tiktokstudio/upload" in r.request.url:
                body = "<textarea>original private draft</textarea>"
            else:
                body = '<a data-e2e="nav-profile" href="/@pilot_owner/">Profile</a>'
            await r.fulfill(body=body, content_type="text/html")

        await context.route("https://www.tiktok.com/**", route)
        original = await context.new_page()
        await original.goto("https://www.tiktok.com/tiktokstudio/upload")
        proof = await context.new_page()
        result = await WebAdapter("tiktok").review_delivery(
            proof,
            {},
            {"body": "Owned caption fixture-marker", "marker": "fixture-marker"},
            url,
            "pilot_owner",
        )
        await proof.close()
        assert result == {"state": "under_review", "visibility": "private"}
        assert (
            await original.locator("textarea").input_value() == "original private draft"
        )
        assert original.url.endswith("/upload") and len(context.pages) == 1
        assert all(method == "GET" for method, _ in requests)


