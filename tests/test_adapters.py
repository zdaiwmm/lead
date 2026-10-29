"""Actual browser contract tests. No real social account or platform write.

Fixtures are independently maintained markup contracts, not selectors generated
from production specs. Native upload bytes, field entry and receipts are checked.
"""

import asyncio
import io
import json
import os
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from lead_app import assets, domain
from lead_app.adapters import get_adapter
from lead_app.adapters.catalog import SPECS, target_url, validate
from lead_app.adapters.core import LayoutChanged, ScopeChanged
from lead_app.browser import Gateway
from lead_app.db import one, tx
from PIL import Image
from starlette.datastructures import UploadFile
from test_assets import video

ROOT = Path(__file__).parent / "fixtures/social"
CHANNEL = "UC1234567890123456789012"
TARGETS = {
    "telegram": "https://web.telegram.org/k/?account=2#-20002",
    "facebook": "https://www.facebook.com/profile.php?id=10002",
    "instagram": "https://www.instagram.com/lead_owner/",
    "youtube": f"https://www.youtube.com/channel/{CHANNEL}/",
    "tiktok": "https://www.tiktok.com/@lead_owner/",
    "x": "https://x.com/lead_owner/",
    "linkedin": "https://www.linkedin.com/in/lead_owner/",
    "reddit": "https://www.reddit.com/r/lead_sub/",
    "pinterest": "https://www.pinterest.com/lead_owner/lead-board/",
    "threads": "https://www.threads.com/@lead_owner/",
    "whatsapp": "https://www.whatsapp.com/channel/LeadChannel123/",
}


class FixtureSite:
    def __init__(self, platform, owner="lead_owner"):
        self.platform = platform
        self.owner = owner
        self.posts = []
        self.uploads = []
        self.requests = []

    async def route(self, route):
        request = route.request
        self.requests.append((request.method, request.url))
        if "/__fixture/upload" in request.url:
            raw = request.post_data_buffer
            message = BytesParser(policy=policy.default).parsebytes(
                (
                    "Content-Type: "
                    + request.headers["content-type"]
                    + "\r\nMIME-Version: 1.0\r\n\r\n"
                ).encode()
                + raw
            )
            files = [
                part.get_payload(decode=True) for part in message.iter_attachments()
            ]
            self.uploads.append(files)
            return await route.fulfill(json={"ok": True})
        if "/__fixture/publish" in request.url:
            body = request.post_data_json
            self.posts.append({**body, "number": 1000000000 + len(self.posts)})
            return await route.fulfill(json=self.posts)
        html = (
            (ROOT / (self.platform + ".html"))
            .read_text()
            .replace("__POSTS__", json.dumps(self.posts).replace("<", "\\u003c"))
        )
        return await route.fulfill(
            body=html.replace("lead_owner", self.owner), content_type="text/html"
        )


@pytest.fixture
async def context(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    gateway = Gateway()
    try:
        yield aid, await gateway.get(aid)
    finally:
        await gateway.close(aid)
        domain.set_control(aid, "archive")


async def sample(aid, kind, tmp_path):
    if kind == "image":
        buffer = io.BytesIO()
        Image.new("RGB", (96, 64), (25, 100, 70)).save(buffer, "PNG")
        data = buffer.getvalue()
    else:
        data = video(tmp_path)
    item = await assets.upload(
        aid,
        UploadFile(
            file=io.BytesIO(data),
            filename="sample." + ("png" if kind == "image" else "mp4"),
        ),
        {"source": "test_owner", "ai_generated": False},
    )
    return assets.resolve(aid, str(item["id"]))


CASES = [(p, k) for p in TARGETS for k in ("image", "video") if k in SPECS[p].formats]


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="real browser website contract suite",
)
@pytest.mark.parametrize("platform,kind", CASES)
async def test_every_platform_native_media_upload_and_publish_contract(
    context, tmp_path, platform, kind
):
    aid, actor = context
    page = actor.page
    site = FixtureSite(platform)
    await page.route("https://**/*", site.route)
    if platform == "facebook":
        await page.context.add_cookies(
            [{"name": "c_user", "value": "10001", "url": "https://www.facebook.com"}]
        )
    entry = "https://web.whatsapp.com/" if platform == "whatsapp" else TARGETS[platform]
    await page.goto(entry)
    adapter = get_adapter(platform)
    account = await adapter.account(page)
    target = await adapter.target(
        page, TARGETS[platform], account["external_account_id"]
    )
    row, path = await sample(aid, kind, tmp_path)
    body = "Browser contract sample lead-fixture-marker"
    publication = {
        "body": body,
        "title": "Browser sample lead-fixture-marker",
        "marker": "lead-fixture-marker",
        "made_for_kids": False,
        "commercial_disclosure": "own_brand",
        "link": "https://example.com/?lead_ref=lead-fixture-marker",
    }
    observation = await adapter.prepare(page, target, publication, [row], [path])
    assert len(site.uploads) == 1 and site.uploads[0] == [path.read_bytes()]
    assert not site.posts  # Preparation can upload a private draft, never publish.
    await adapter.before_commit(
        page, target, observation, account["external_account_id"]
    )
    await adapter.submit(page, observation)
    result = None
    for _ in range(30):
        result = await adapter.receipt(
            page, target, publication, observation, account["external_account_id"]
        )
        if result:
            break
        await asyncio.sleep(0.05)
    assert result and result["external_id"]
    assert len(site.posts) == 1
    assert site.posts[0]["files"][0]["sha256"] == row["sha256"]
    assert site.posts[0]["files"][0]["type"] == row["media_type"]
    assert (
        site.posts[0]["body"] == body
        or platform == "reddit"
        and site.posts[0]["title"] == publication["title"]
    )
    await adapter.reconcile(
        page, target, publication, observation, account["external_account_id"]
    )
    assert len(site.posts) == 1 and len(site.uploads) == 1


def test_capability_matrix_and_untrusted_targets():
    assert set(SPECS) == {"telegram", *TARGETS}
    for p, url in TARGETS.items():
        assert target_url(p, url)
    for p in TARGETS:
        for url in [
            "http://127.0.0.1/",
            "https://evil.example/target",
            "https://" + SPECS[p].host + "@127.0.0.1/a",
        ]:
            with pytest.raises(ValueError):
                target_url(p, url)
    with pytest.raises(ValueError):
        validate("instagram", {"body": "caption"}, [])
    with pytest.raises(ValueError):
        validate("tiktok", {"body": "caption"}, [])
    assert (
        SPECS["x"].policy == "restricted" and SPECS["linkedin"].policy == "restricted"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1", reason="real browser ledger recovery"
)
@pytest.mark.parametrize("failure_point", ["ack", "prepare"])
async def test_media_lost_ack_uses_ledger_and_never_uploads_or_clicks_twice(
    context, tmp_path, monkeypatch, failure_point
):
    from lead_app import channels, social_publish
    from lead_app.activities import prepare_action
    from lead_app.config import TENANT
    from lead_app.db import require_agent

    aid, actor = context
    page = actor.page
    site = FixtureSite("x", owner="lead_" + aid[:8])
    own_target = TARGETS["x"].replace("lead_owner", site.owner)
    await page.route("https://**/*", site.route)
    await page.goto(own_target)
    adapter = get_adapter("x")
    account = await adapter.account(page)
    observed = await adapter.target(page, own_target, account["external_account_id"])
    with tx(aid) as c:
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        saved = one(
            c,
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,external_account_id,state) VALUES(%s,%s,%s,'x',%s,'connected') RETURNING id",
            (TENANT, aid, profile["id"], account["external_account_id"]),
        )
    target = channels.store_target(aid, str(saved["id"]), observed)
    monkeypatch.setattr(channels, "emit", lambda *a, **k: None)
    channels.accept_card(aid, str(target["card_id"]), 1, True)
    asset, path = await sample(aid, "image", tmp_path)
    card = channels.create_verification(
        aid, str(target["target_id"]), [str(asset["id"])]
    )
    channels.accept_card(aid, str(card["card_id"]), 1, True)
    with tx(aid) as c:
        task = one(c, "SELECT * FROM lead.tasks ORDER BY created_at DESC LIMIT 1")
    action = prepare_action(aid, str(task["id"]))
    # Policy is deliberately restricted in production. The fixture bypasses only
    # that external permission review; actor control/grant/lease fences stay real.
    monkeypatch.setattr(
        social_publish, "gate", lambda a, c, x: require_agent(c, aid, True)
    )
    monkeypatch.setattr(social_publish, "get_adapter", lambda platform: adapter)
    if failure_point == "prepare":
        prepare = adapter.prepare

        async def interrupted(*args, **kwargs):
            await prepare(*args, **kwargs)
            raise ConnectionError("fixture interrupted after private draft upload")

        monkeypatch.setattr(adapter, "prepare", interrupted)
        with pytest.raises(ConnectionError):
            await social_publish.execute(actor, str(action["id"]))
        monkeypatch.setattr(adapter, "prepare", prepare)
        resumed = await social_publish.execute(actor, str(action["id"]))
        assert resumed["state"] == "blocked"
        assert len(site.uploads) == 1 and not site.posts
        with tx(aid) as c:
            assert (
                one(c, "SELECT stage FROM lead.publish_preparations")["stage"]
                == "uncertain"
            )
            assert not one(c, "SELECT id FROM lead.action_attempts")
        return
    receipt = adapter.receipt

    async def lost(*args, **kwargs):
        for _ in range(100):
            if site.posts:
                break
            await asyncio.sleep(0.01)
        raise ConnectionError("lost fixture acknowledgement")

    monkeypatch.setattr(adapter, "receipt", lost)
    assert (await social_publish.execute(actor, str(action["id"])))[
        "state"
    ] == "unknown"
    assert len(site.posts) == 1 and len(site.uploads) == 1
    monkeypatch.setattr(adapter, "receipt", receipt)
    second = await social_publish.execute(actor, str(action["id"]))
    assert second["state"] == "confirmed" and second["reconciled"]
    third = await social_publish.execute(actor, str(action["id"]))
    assert third["state"] == "confirmed"
    assert len(site.posts) == 1 and len(site.uploads) == 1
    with tx(aid) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.action_attempts")["n"] == 1
        assert (
            one(c, "SELECT stage FROM lead.publish_preparations")["stage"]
            == "submitted"
        )
        assert domain.metrics(c)["published"] == 0  # Connection probe is excluded.


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1", reason="real browser scope guards"
)
async def test_changed_identity_or_layout_cannot_cross_commit_boundary(
    context, tmp_path
):
    aid, actor = context
    page = actor.page
    site = FixtureSite("x")
    await page.route("https://**/*", site.route)
    await page.goto(TARGETS["x"])
    adapter = get_adapter("x")
    own = await adapter.account(page)
    target = await adapter.target(page, TARGETS["x"], own["external_account_id"])
    asset, path = await sample(aid, "image", tmp_path)
    post = {
        "body": "Known lead-fixture-marker",
        "marker": "lead-fixture-marker",
        "title": "Known",
    }
    prepared = await adapter.prepare(page, target, post, [asset], [path])
    await page.locator("[data-testid=tweetTextarea_0]").fill(
        "Unexpected edited content"
    )
    with pytest.raises(ScopeChanged):
        await adapter.before_commit(page, target, prepared, own["external_account_id"])
    assert not site.posts
    await page.locator("[data-testid=tweetTextarea_0]").fill(post["body"])
    await page.locator("[data-testid=AppTabBar_Profile_Link]").evaluate(
        "el=>el.href='https://x.com/another_owner'"
    )
    with pytest.raises(ScopeChanged):
        await adapter.before_commit(page, target, prepared, own["external_account_id"])
    assert not site.posts
    await page.locator("[data-testid=AppTabBar_Profile_Link]").evaluate(
        "el=>el.href='https://x.com/lead_owner'"
    )
    await page.locator("[data-testid=tweetButton]").evaluate("el=>el.remove()")
    with pytest.raises(LayoutChanged):
        await adapter.before_commit(page, target, prepared, own["external_account_id"])
    assert not site.posts
    # A link to the owner inside somebody else's post is not author evidence.
    from lead_app.adapters.core import scoped_receipt

    await page.evaluate("""() => {
      const post=document.createElement('article');
      post.innerHTML='<header><a href="https://x.com/another_owner">Other</a></header><p>Known lead-fixture-marker</p><a href="https://x.com/lead_owner">Quoted profile</a><a href="https://x.com/another_owner/status/123">Post</a>';
      document.body.append(post);
    }""")
    assert (
        await scoped_receipt(
            page, "article", r"/status/\d+", post, "lead_owner", r"https://x.com/[^/]+$"
        )
        is None
    )


def test_policy_and_media_format_never_promoted_by_adapter_tests():
    from lead_app.db import all_rows

    with tx() as c:
        rows = all_rows(
            c,
            "SELECT platform,permission_state,technical_state FROM lead.platform_capabilities WHERE platform IN ('x','linkedin')",
        )
    assert rows and all(
        r["permission_state"] == "restricted" and r["technical_state"] != "verified"
        for r in rows
    )
