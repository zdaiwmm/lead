"""Real receipt DOM provenance; no model, actual publication, or B reconstruction."""
import copy
import os

import pytest
from test_native_declarative_forms_dom import ORIGIN
from test_native_receipt_indexes_dom import (
    common_page,  # noqa: F401
    receipt_controller,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="Explicit isolated receipt author provenance DOM verification required")]


def index_article(author_url, post_url, *, extra="", author_rel=""):
    return (f'<article><h2><a href="{post_url}">Entry</a></h2>'
        f'<address>By <a href="{author_url}" rel="{author_rel}">member-A</a></address>'
        '<div>A source excerpt</div>'
        f'<p><time>Today</time><a rel="bookmark" href="{post_url}">Permalink</a></p>{extra}</article>')


async def author_receipts(observed_page, monkeypatch, *, extra="", author_rel="", author_url=None,
                          author_markup=None, second_matches=False, root_extra=""):
    controller, attempt, state, visited, page, root, index = await receipt_controller(observed_page, monkeypatch)
    _, context, _ = observed_page
    author_url = author_url or ORIGIN + "/members/new-author"
    second = ORIGIN + "/records/66"
    await context.unroute("**/*")

    async def route(current):
        url = current.request.url
        if url == root:
            html = '<nav><a href="/catalog">Read entries</a></nav>' + (root_extra if state["new"] else "")
        elif url == index:
            html = index_article(ORIGIN + "/members/old-author", ORIGIN + "/records/44")
            if state["new"]:
                html += (author_markup if author_markup is not None else
                    index_article(author_url, ORIGIN + "/records/55", extra=extra, author_rel=author_rel))
        elif url == ORIGIN + "/records/55" or second_matches and url == second:
            html = ('<article><p>Author: <span id="author">member-A</span></p>'
                f'<div id="body">New answer</div><a id="url" rel="canonical" href="{url}">Canonical</a></article>')
        else:
            html = '<main>A member profile, without any receipt article.</main>'
        await current.fulfill(body=html, content_type="text/html")

    await context.route("**/*", route)
    await controller.baseline()
    frozen = copy.deepcopy(attempt["observation"]["browser_baseline"])
    assert ORIGIN + "/members/old-author" in frozen["urls"], "baseline must retain every observed author URL"
    state.update(new=True, action_state="unknown")
    await page.goto(ORIGIN + "/records/55")
    controller.page = page
    controller.reconcile = True
    return controller, attempt, state, visited, frozen, author_url, root, index


async def test_new_strong_author_page_does_not_make_the_unique_receipt_unprovable(common_page, monkeypatch):  # noqa: F811
    controller, attempt, state, visited, frozen, author_url, _, _ = await author_receipts(common_page, monkeypatch)
    result = await controller.report_receipt({"selectors": {"author": "#author", "body": "#body", "url": "#url"}})
    assert result["state"] == "confirmed"
    assert attempt["attempt"] == 1 and state["action_state"] == "confirmed"
    assert attempt["observation"]["browser_baseline"] == frozen
    assert author_url not in visited, "positive author provenance, not a None receipt read, must exclude this link"
