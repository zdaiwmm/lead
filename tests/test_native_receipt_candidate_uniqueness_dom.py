"""Real receipt DOM counterexamples; no model, real DB, publication or platform.

A model's current-receipt selector cannot prove that another new candidate is
not a duplicate. Only the final original-attempt/action writes are in memory;
baseline discovery, candidate navigation, read_receipt and report_receipt run.
"""
import os

import pytest
from test_native_receipt_indexes_dom import common_page as shared_page
from test_native_receipt_indexes_dom import receipt_controller

common_page = shared_page
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="Explicit isolated Chromium DOM verification required")]


@pytest.mark.parametrize(("candidate", "selector_style"), [
    ("duplicate", "shared"),
    ("duplicate", "fixed_id"),
    ("duplicate", "fixed_permalink"),
    ("unreadable", "shared"),
    ("other_body", "shared"),
    ("other_author", "shared"),
])
async def test_current_receipt_selector_cannot_silently_exclude_an_unread_candidate(
        common_page, monkeypatch, tmp_path, candidate, selector_style):
    from lead_app import config
    from test_native_declarative_forms_dom import ORIGIN

    controller, attempt, state, visited, page, root, index = await receipt_controller(common_page, monkeypatch)
    monkeypatch.setattr(config, "STATE", tmp_path)
    context = common_page[1]
    first, second = ORIGIN+"/records/55", ORIGIN+"/records/56"
    network = []

    async def route(current):
        network.append((current.request.method, current.request.url))
        assert current.request.method == "GET", "this regression must never submit a publication"
        url = current.request.url
        if url == root:
            html = '<nav><a href="/catalog">Read entries</a></nav>'
        elif url == index:
            html = '<main><a href="/records/44">Previous entry</a>'
            if state["new"]:
                html += '<a href="/records/55">New entry one</a><a href="/records/56">New entry two</a>'
            html += '</main>'
        elif url in (first, second):
            identifier = "note-7001" if url == first else "note-7002"
            author = "member-B" if url == second and candidate == "other_author" else "member-A"
            body = "A different answer" if url == second and candidate == "other_body" else "New answer"
            label = "Member" if url == second and candidate == "unreadable" else "Author"
            html = (f'<article id="{identifier}"><p>{label}: <span class="by">{author}</span></p>'
                f'<div class="text">{body}</div><a class="canonical" rel="canonical" href="{url}">Entry URL</a></article>')
        else:
            html = '<main>Previous entry</main>'
        await current.fulfill(body=html, content_type="text/html")

    await context.unroute("**/*")
    await context.route("**/*", route)
    await controller.baseline()
    baseline = attempt["observation"]["browser_baseline"]
    assert baseline["bounded"] and baseline["index_pages"] == [root, index]
    assert first not in baseline["urls"] and second not in baseline["urls"]
    state.update(new=True, action_state="unknown")
    await page.goto(first)
    controller.page, controller.reconcile = page, True
    shared = {"author": "article .by", "body": "article .text", "url": "article a.canonical"}
    selectors = dict(shared)
    if selector_style == "fixed_id":
        selectors = {name: "#note-7001 "+value.removeprefix("article ") for name, value in shared.items()}
    elif selector_style == "fixed_permalink":
        selectors["url"] = f'article a[href="{first}"]'
    assert await controller.read_receipt(page, selectors)
    observed = await context.new_page()
    try:
        await observed.goto(second)
        if candidate == "duplicate":
            # The second receipt is independently readable as the same original
            # author/body. The model's first-ID/URL selectors simply miss it.
            value = await controller.read_receipt(observed, shared)
            assert value and value["url"] == second and value["body"] == controller.action["intent"]["body"]
            if selector_style != "shared":
                assert await controller.read_receipt(observed, selectors) is None
        elif candidate in {"other_body", "other_author"}:
            assert await observed.locator("article .by").inner_text() == (
                "member-B" if candidate == "other_author" else "member-A")
            assert await observed.locator("article .text").inner_text() == (
                "A different answer" if candidate == "other_body" else "New answer")
            assert await controller.read_receipt(observed, shared) is None
        else:
            assert await controller.read_receipt(observed, shared) is None
    finally:
        await observed.close()

    visited.clear()
    if candidate in {"duplicate", "unreadable"}:
        with pytest.raises(ValueError, match="browser_receipt_(not_unique|unproved|candidate_unproved)"):
            await controller.report_receipt({"selectors": selectors})
        assert state["action_state"] == "unknown" and attempt["outcome"] is None
    else:
        # A second candidate proven to have another body/author may be excluded;
        # rejecting every two-candidate set would discard legitimate receipts.
        result = await controller.report_receipt({"selectors": selectors})
        assert result["state"] == "confirmed" and state["action_state"] == "confirmed"
    assert first in visited and second in visited
    assert attempt["observation"]["browser_baseline"] == baseline
    assert all(method == "GET" for method, _ in network)
