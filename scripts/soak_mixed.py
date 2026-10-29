"""Mixed native-browser phases, isolated to the endurance workspace.

The social pages are local markup contracts, never production social accounts.
"""

import asyncio
import io
import os
import sys
import tempfile
import time
from pathlib import Path
from uuid import UUID

from lead_app import assets, domain
from lead_app.activities import prepare_action
from lead_app.adapters import get_adapter
from lead_app.browser import Gateway
from lead_app.config import ROOT, STATE
from lead_app.db import one, tx
from PIL import Image
from starlette.datastructures import UploadFile

# Reuse independent website contracts, not markup generated from adapter specs.
sys.path.insert(0, str(ROOT / "tests"))
from test_adapters import CASES, TARGETS, FixtureSite
from test_assets import video


async def run(client, report, number):
    ids = report["agent_ids"]
    for aid in ids:
        (await client.post("/agents/" + aid + "/close")).raise_for_status()
    gateway = Gateway()
    intervals = []
    counts = {}
    media = report.setdefault("mixed_assets", {})
    try:
        with tempfile.TemporaryDirectory(prefix="lead-mixed-") as temporary:
            for aid in ids:
                if aid not in media:
                    buffer = io.BytesIO()
                    Image.new("RGB", (256, 256), (50, 100, 70)).save(buffer, "PNG")
                    media[aid] = {}
                    for kind, data in (
                        ("image", buffer.getvalue()),
                        ("video", video(Path(temporary))),
                    ):
                        item = await assets.upload(
                            aid,
                            UploadFile(
                                file=io.BytesIO(data),
                                filename="mixed."
                                + ("png" if kind == "image" else "mp4"),
                            ),
                            {"source": "verification", "ai_generated": False},
                        )
                        media[aid][kind] = str(item["id"])
        actors = await asyncio.gather(*(gateway.get(aid) for aid in ids))
        waiting = asyncio.Event()
        secondary_finished = asyncio.Event()
        done = set()
        first_upload = asyncio.Barrier(3)
        upload_intervals = []

        async def uploads(index, actor):
            start = time.monotonic()
            for step, (platform, kind) in enumerate(CASES[index::3]):
                page = actor.page
                site = FixtureSite(platform)
                await page.route("https://**/*", site.route)
                try:
                    if platform == "facebook":
                        await page.context.add_cookies(
                            [
                                {
                                    "name": "c_user",
                                    "value": "10001",
                                    "url": "https://www.facebook.com",
                                }
                            ]
                        )
                    await page.goto(
                        "https://web.whatsapp.com/"
                        if platform == "whatsapp"
                        else TARGETS[platform]
                    )
                    adapter = get_adapter(platform)
                    account = await adapter.account(page)
                    target = await adapter.target(
                        page, TARGETS[platform], account["external_account_id"]
                    )
                    item, path = assets.resolve(actor.aid, media[actor.aid][kind])
                    publication = {
                        "body": "Controlled mixed browser lead-fixture-marker",
                        "title": "Mixed sample lead-fixture-marker",
                        "marker": "lead-fixture-marker",
                        "made_for_kids": False,
                        "commercial_disclosure": "own_brand",
                        "link": "https://example.com/?lead_ref=lead-fixture-marker",
                    }
                    if step == 0:
                        await first_upload.wait()
                    upload_start = time.monotonic()
                    prepared = await adapter.prepare(
                        page, target, publication, [item], [path]
                    )
                    assert site.uploads == [[path.read_bytes()]] and not site.posts
                    if step == 0:
                        upload_intervals.append((upload_start, time.monotonic()))
                    await adapter.before_commit(
                        page, target, prepared, account["external_account_id"]
                    )
                    await adapter.submit(page, prepared)
                    receipt = None
                    for _ in range(30):
                        receipt = await adapter.receipt(
                            page,
                            target,
                            publication,
                            prepared,
                            account["external_account_id"],
                        )
                        if receipt:
                            break
                        await asyncio.sleep(0.05)
                    assert (
                        receipt and receipt["external_id"] and len(site.posts) == 1
                    ), (platform, kind, "receipt_missing_or_duplicate")
                    counts[platform + ":" + kind] = (
                        counts.get(platform + ":" + kind, 0) + 1
                    )
                finally:
                    await page.unroute("https://**/*", site.route)
                if step == 0:
                    if index == 0:
                        waiting.set()
                        # Controlled response wait, separate from provider billing.
                        await secondary_finished.wait()
                    else:
                        await waiting.wait()
            intervals.append(
                {"agent_id": actor.aid, "start": start, "end": time.monotonic()}
            )
            if index:
                done.add(index)
                if len(done) == 2:
                    secondary_finished.set()

        await asyncio.wait_for(
            asyncio.gather(*(uploads(i, actor) for i, actor in enumerate(actors))),
            timeout=180,
        )
        assert len(counts) == len(CASES)
        assert len(upload_intervals) == 3 and max(s for s, e in upload_intervals) < min(
            e for s, e in upload_intervals
        )
        assert max(i["start"] for i in intervals) < min(i["end"] for i in intervals)
        # Lose the acknowledgement after the actual local submit, then rebuild
        # the browser and reconcile the same immutable operation without clicking.
        aid = ids[0]
        old_emit = domain.emit
        try:
            domain.emit = lambda *a, **k: None  # this verifier process only
            task = domain.queue_lab_publish(
                aid,
                "Mixed lost acknowledgement " + str(number),
                report["run_id"] + ":ack:" + str(number),
            )
        finally:
            domain.emit = old_emit
        action = prepare_action(aid, str(task["id"]))
        result = await actors[0].publish_lab(str(action["id"]), fault=True)
        assert result["state"] == "unknown"
        await gateway.close(aid)
        actor = await gateway.get(aid)
        result = await actor.publish_lab(str(action["id"]))
        assert result["state"] == "confirmed" and result["reconciled"]
        with tx(aid) as c:
            assert (
                one(
                    c,
                    "SELECT count(*) AS n FROM lead.lab_posts WHERE operation_key=%s",
                    (action["operation_key"],),
                )["n"]
                == 1
            )
            c.execute(
                "UPDATE lead.tasks SET state='succeeded',completed_at=now() WHERE id=%s",
                (task["id"],),
            )
        report["mixed_contracts"] = counts
        report["mixed_intervals"] = intervals
        report["native_upload_overlap"] = upload_intervals
        report["lost_ack_reconciliations"] = (
            report.get("lost_ack_reconciliations", 0) + 1
        )
    finally:
        await asyncio.gather(*(gateway.close(aid) for aid in ids))
    if number == 1 or number % 12 == 0:
        log = STATE / "endurance" / (report["run_id"] + "-faults.log")
        with log.open("a") as stream:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                "tests/test_failure_isolation.py",
                "tests/test_browser.py::test_browser_process_crash_reopens_same_profile",
                "tests/test_workflows.py",
                cwd=ROOT,
                env={
                    **os.environ,
                    "LEAD_BROWSER_TESTS": "1",
                    "LEAD_WORKFLOW_TESTS": "1",
                    "LEAD_VERIFICATION_TENANT": report["tenant_id"],
                },
                stdout=stream,
                stderr=stream,
            )
            try:
                code = await asyncio.wait_for(process.wait(), 150)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert code == 0, "isolated_transport_crash_and_workflow_recovery_failed"
        report["isolated_fault_checks"] = report.get("isolated_fault_checks", 0) + 1
    # Exercise the real API implementation and HMAC browser gateway in this
    # private scope. A stays paused throughout takeover; B/C keep working.
    from lead_app.api import (
        BrowserInput,
        Connection,
        browser_input,
        connect,
        end_takeover,
        screenshot,
    )

    aid = ids[0]
    await connect(UUID(aid), Connection(platform="lab"))
    try:
        assert (await screenshot(UUID(aid))).body[:8] == b"\x89PNG\r\n\x1a\n"
        await browser_input(UUID(aid), BrowserInput(kind="scroll", y=60))
        tasks = []
        for other in ids[1:]:
            queued = domain.queue_lab_publish(
                other,
                "Other Agent during takeover " + str(number),
                report["run_id"] + ":takeover:" + str(number),
            )
            tasks.append((other, queued["id"]))
        for _ in range(180):
            states = []
            for other, tid in tasks:
                with tx(other) as c:
                    states.append(
                        one(c, "SELECT state FROM lead.tasks WHERE id=%s", (tid,))[
                            "state"
                        ]
                    )
            if states == ["succeeded", "succeeded"]:
                break
            await asyncio.sleep(0.5)
        assert states == ["succeeded", "succeeded"]
        with tx(aid) as c:
            assert (
                one(c, "SELECT desired_state FROM lead.agents")["desired_state"]
                == "paused"
            )
            assert one(c, "SELECT 1 FROM lead.takeover_sessions WHERE state='active'")
    finally:
        end_takeover(UUID(aid))
        (await client.post("/agents/" + aid + "/close")).raise_for_status()
        with tx(aid) as c:
            # Test-only reset of its private fixture; no production permission gate.
            c.execute(
                "UPDATE lead.agents SET desired_state='draft' WHERE id=%s", (aid,)
            )
    report["takeover_checks"] = report.get("takeover_checks", 0) + 1
    report["controlled_model_wait_checks"] = (
        report.get("controlled_model_wait_checks", 0) + 1
    )
    report["native_media_uploads"] = report.get("native_media_uploads", 0) + len(CASES)
    report["profile_reopens"] = report.get("profile_reopens", 0) + 1
