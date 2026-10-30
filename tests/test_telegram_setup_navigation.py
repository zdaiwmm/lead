"""Real browser, in-memory HTML, no account/profile or external submission."""

import os

import pytest
import pytest_asyncio
from lead_app import telegram

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        os.environ.get("LEAD_BROWSER_TESTS") != "1",
        reason="real Camoufox against in-memory local HTML only",
    ),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    from camoufox.async_api import AsyncCamoufox
    from lead_app.config import BROWSER_BUILD

    async with AsyncCamoufox(browser=BROWSER_BUILD, headless=True) as instance:
        yield instance


@pytest.mark.parametrize("selected", [False, True])
async def test_invite_sidebar_exits_without_clicking_next(browser, selected):
    page = await browser.new_page()
    try:
        await page.set_content(
            """<div class="add-members-container">
            <div class="sidebar-header"><button class="sidebar-close-button"
            onclick="document.body.dataset.inviteSidebarClosed=true;this.closest('.add-members-container').remove()">Back</button></div>
            <input type="checkbox" %s>
            <button class="btn-corner is-visible" onclick="document.body.dataset.invited=true">Next</button>
            </div>"""
            % ("checked" if selected else "")
        )
        assert await telegram.find_prepared_channel(page, {"title": "Fixture"}) is None
        assert (
            await page.locator("body").get_attribute("data-invite-sidebar-closed")
            == "true"
        )
        assert await page.locator("body").get_attribute("data-invited") is None
    finally:
        await page.close()


async def test_unknown_creation_cancels_invite_confirmation_before_reconciliation(
    browser,
):
    page = await browser.new_page()
    try:
        await page.set_content(
            """<div class="popup popup-peer popup-add-members active old"
            style="position:fixed;inset:0;z-index:10;background:white">
            Add member: Are you sure?
            <button onclick="document.body.dataset.cancelled=true;setTimeout(()=>this.parentElement.remove(),50)">CANCEL</button>
            <button onclick="document.body.dataset.invited=true">ADD</button></div>
            <div class="add-members-container"><div class="sidebar-header">
            <button class="sidebar-close-button" onclick="document.body.dataset.left=true;this.closest('.add-members-container').remove()">Back</button>
            </div><button class="btn-corner is-visible" onclick="document.body.dataset.invited=true">Next</button></div>"""
        )
        assert await telegram.find_prepared_channel(page, {"title": "Fixture"}) is None
        assert await page.locator("body").get_attribute("data-cancelled") == "true"
        assert await page.locator("body").get_attribute("data-left") == "true"
        assert await page.locator("body").get_attribute("data-invited") is None
    finally:
        await page.close()


async def test_existing_channel_is_reconciled_after_cancel_without_new_creation(
    browser, monkeypatch
):
    page = await browser.new_page()
    inspected = []
    observed = {"external_target_id": "-54321"}

    async def inspect_account(page):
        return {"slot": "2", "external_account_id": "fixture-owner"}

    async def inspect_target(page, url, expected_account):
        assert url == "https://web.telegram.org/k/?account=2#-54321"
        assert expected_account == "fixture-owner"
        inspected.append(url)
        return observed

    async def own_profile(page, setup):
        assert setup["title"] == "Fixture"

    monkeypatch.setattr(telegram, "inspect_account", inspect_account)
    monkeypatch.setattr(telegram, "inspect_target", inspect_target)
    monkeypatch.setattr(telegram, "_own_channel_profile", own_profile)
    try:
        await page.set_content(
            """<div class="chatlist"><div data-peer-id="-54321">
            <span class="peer-title">Fixture</span></div></div>
            <div class="popup-add-members"><button
            onclick="this.parentElement.remove()">取消</button><button
            onclick="document.body.dataset.invited=true">ADD</button></div>
            <button onclick="document.body.dataset.created=true">Create channel</button>"""
        )
        for _ in range(2):
            assert (
                await telegram.find_prepared_channel(page, {"title": "Fixture"})
                == observed
            )
        assert len(inspected) == 2
        assert await page.locator("body").get_attribute("data-invited") is None
        assert await page.locator("body").get_attribute("data-created") is None
    finally:
        await page.close()


@pytest.mark.parametrize(
    "html",
    [
        '<div class="popup-add-members"><button>ADD</button></div>',
        '<div class="popup-add-members"><button>Cancel</button><button>Cancel</button></div>',
        '<div class="popup-add-members"><button>Cancel</button></div>'
        '<div class="popup-add-members"><button>Cancel</button></div>',
        '<div class="add-members-container"><button class="btn-corner is-visible" '
        'onclick="document.body.dataset.invited=true">Next</button></div>',
    ],
)
async def test_changed_or_ambiguous_navigation_blocks_without_inviting(browser, html):
    page = await browser.new_page()
    try:
        await page.set_content(html)
        with pytest.raises(telegram.TelegramSetupBlocked):
            await telegram.skip_new_channel_members(page)
        assert await page.locator("body").get_attribute("data-invited") is None
    finally:
        await page.close()


async def test_other_dialog_is_not_dismissed(browser):
    page = await browser.new_page()
    try:
        await page.set_content(
            '<div class="popup-delete-channel"><button '
            'onclick="document.body.dataset.clicked=true">Cancel</button></div>'
        )
        await telegram.skip_new_channel_members(page)
        assert await page.locator(".popup-delete-channel").is_visible()
        assert await page.locator("body").get_attribute("data-clicked") is None
    finally:
        await page.close()
