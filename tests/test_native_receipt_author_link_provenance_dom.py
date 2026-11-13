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


PROFILE = '<title>Member profile</title><main><h1>Member profile</h1><p>Member: member-A</p></main>'
SELECTORS = {"author": "#author", "body": "#body", "url": "#url"}


async def author_receipts(observed_page, monkeypatch, *, extra="", author_rel="", author_url=None,
                          author_markup=None, second_matches=False, root_extra="", profile_html=PROFILE,
                          second_profile_heading=False, old_author_receipt=False):
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
        elif (url == ORIGIN + "/records/55" or second_matches and url == second
                or state["new"] and old_author_receipt and url == ORIGIN + "/members/old-author"):
            html = ((PROFILE if second_profile_heading and url == second else "") +
                '<article><p>Author: <span id="author">member-A</span></p>'
                f'<div id="body">New answer</div><a id="url" rel="canonical" href="{url}">Canonical</a></article>')
        else:
            html = profile_html
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
    result = await controller.report_receipt({"selectors": SELECTORS})
    assert result["state"] == "confirmed"
    assert attempt["attempt"] == 1 and state["action_state"] == "confirmed"
    assert attempt["observation"]["browser_baseline"] == frozen
    assert author_url in visited, "the author destination needs fresh positive profile proof"


@pytest.mark.parametrize("where", ["ordinary", "permalink", "cross_index", "navigation"])
async def test_any_non_author_occurrence_keeps_the_same_url_a_candidate(common_page, monkeypatch, where):  # noqa: F811
    url = ORIGIN + "/members/new-author"
    extra = f'<p><a href="{url}">Another page</a></p>' if where == "ordinary" else ""
    if where == "permalink":
        extra = f'<p><a href="{url}" aria-label="Permalink">Permalink</a></p>'
    root_extra = f'<a href="{url}">Another page</a>' if where == "cross_index" else ""
    if where == "navigation":
        root_extra = f'<nav><a href="{url}">Browse members</a></nav>'
    controller, attempt, state, _, frozen, _, _, _ = await author_receipts(
        common_page, monkeypatch, extra=extra, root_extra=root_extra)
    with pytest.raises(ValueError, match="browser_receipt_candidate_unproved"):
        await controller.report_receipt({"selectors": SELECTORS})
    assert state["action_state"] == "unknown" and attempt["outcome"] is None
    assert attempt["observation"]["browser_baseline"] == frozen


@pytest.mark.parametrize("rel", ["bookmark", "canonical"])
async def test_an_author_anchor_with_receipt_rel_is_not_excludable(common_page, monkeypatch, rel):  # noqa: F811
    controller, _, state, _, _, _, _, _ = await author_receipts(common_page, monkeypatch, author_rel=rel)
    with pytest.raises(ValueError, match="browser_receipt_candidate_unproved"):
        await controller.report_receipt({"selectors": SELECTORS})
    assert state["action_state"] == "unknown"


@pytest.mark.parametrize("profile_heading", [False, True])
async def test_author_only_url_with_a_second_complete_matching_receipt_cannot_be_hidden(common_page, monkeypatch, profile_heading):  # noqa: F811
    second = ORIGIN + "/records/66"
    controller, attempt, state, visited, frozen, _, _, _ = await author_receipts(
        common_page, monkeypatch, author_url=second, second_matches=True, second_profile_heading=profile_heading)
    with pytest.raises(ValueError, match="browser_receipt_not_unique"):
        await controller.report_receipt({"selectors": SELECTORS})
    assert second in visited
    assert state["action_state"] == "unknown" and attempt["outcome"] is None
    assert attempt["observation"]["browser_baseline"] == frozen


@pytest.mark.parametrize("wrapper", ["blockquote", "form", 'div contenteditable="true"'])
async def test_ugc_author_markup_is_not_positive_author_provenance(common_page, monkeypatch, wrapper):  # noqa: F811
    tag = wrapper.split()[0]
    url = ORIGIN + "/members/new-author"
    markup = ('<article><h2>Entry</h2><div class="user-content">'
        f'<{wrapper}><address>By <a href="{url}">member-A</a></address></{tag}></div>'
        '<p><time>Today</time><a rel="bookmark" href="/records/55">Permalink</a></p></article>')
    controller, _, state, _, _, _, _, _ = await author_receipts(common_page, monkeypatch, author_markup=markup)
    with pytest.raises(ValueError, match="browser_receipt_candidate_unproved"):
        await controller.report_receipt({"selectors": SELECTORS})
    assert state["action_state"] == "unknown"


async def test_ugc_fake_author_cannot_hide_a_second_actual_matching_receipt(common_page, monkeypatch):  # noqa: F811
    second = ORIGIN + "/records/66"
    markup = ('<article><h2>Entry</h2><blockquote><address>By '
        f'<a href="{second}">member-A</a></address></blockquote>'
        '<p><a rel="bookmark" href="/records/55">Permalink</a></p></article>')
    controller, _, state, visited, _, _, _, _ = await author_receipts(
        common_page, monkeypatch, author_markup=markup, second_matches=True)
    with pytest.raises(ValueError, match="browser_receipt_not_unique"):
        await controller.report_receipt({"selectors": SELECTORS})
    assert second in visited and state["action_state"] == "unknown"


async def test_embedded_article_inside_an_explicit_user_body_cannot_supply_author_provenance(common_page, monkeypatch):  # noqa: F811
    url = ORIGIN + "/members/new-author"
    markup = ('<article><h2>Entry</h2><div itemprop="articleBody">'
        '<article><h3>User-supplied markup</h3><address>By '
        f'<a href="{url}">member-A</a></address>'
        '<p><a rel="bookmark" href="/records/55">Embedded permalink</a></p></article></div>'
        '<p><a rel="bookmark" href="/records/55">Permalink</a></p></article>')
    controller, _, state, _, _, _, _, _ = await author_receipts(common_page, monkeypatch, author_markup=markup)
    with pytest.raises(ValueError, match="browser_receipt_candidate_unproved"):
        await controller.report_receipt({"selectors": SELECTORS})
    assert state["action_state"] == "unknown"


@pytest.mark.parametrize("html", [
    "<main>There is no receipt article.</main>",
    "<title>Member profile</title><main><h1>Member profile</h1><p>Member: another-member</p></main>",
    "<title>Member profile</title><main><h1>Member profile</h1><p>Member: member-AA</p></main>",
    "<title>General page</title><main><p>member-A</p></main>",
])
async def test_none_receipt_never_excludes_a_page_without_positive_exact_profile_identity(common_page, monkeypatch, html):  # noqa: F811
    controller, _, state, _, _, _, _, _ = await author_receipts(common_page, monkeypatch, profile_html=html)
    with pytest.raises(ValueError, match="browser_receipt_candidate_unproved"):
        await controller.report_receipt({"selectors": SELECTORS})
    assert state["action_state"] == "unknown"


async def test_baseline_author_url_cannot_become_a_new_receipt_after_its_role_changes(common_page, monkeypatch):  # noqa: F811
    controller, attempt, state, _, frozen, _, _, _ = await author_receipts(common_page, monkeypatch, old_author_receipt=True)
    await controller.page.goto(ORIGIN + "/members/old-author")
    with pytest.raises(ValueError, match="browser_receipt_preexisting_or_ambiguous"):
        await controller.report_receipt({"selectors": SELECTORS})
    assert state["action_state"] == "unknown" and attempt["outcome"] is None
    assert attempt["observation"]["browser_baseline"] == frozen
