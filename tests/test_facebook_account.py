"""Native browser identity checks against isolated Facebook markup, without login."""

import os

import pytest
import pytest_asyncio
from camoufox.async_api import AsyncCamoufox
from lead_app.adapters import get_adapter
from lead_app.adapters.core import NeedsAuth

pytestmark = [
    pytest.mark.skipif(
        os.getenv("LEAD_BROWSER_TESTS") != "1", reason="requires Camoufox"
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    async with AsyncCamoufox(headless=True) as instance:
        yield instance


@pytest.mark.parametrize(
    "label", ["Your profile", "Account", "你的个人主页", "账户", "你的個人檔案", "帳號"]
)
async def test_home_identity_without_profile_navigation(browser, label):
    context = await browser.new_context()
    await context.add_cookies(
        [{"name": "c_user", "value": "10001", "url": "https://www.facebook.com"}]
    )
    page = await context.new_page()
    requests = []

    async def route(request):
        requests.append(request.request.url)
        await request.fulfill(
            content_type="text/html; charset=utf-8",
            body=f'<button aria-label="{label}">头像</button>'
            '<main><article><a href="/profile.php?id=10002">其他用户</a></article></main>',
        )

    await page.route("**/*", route)
    await page.goto("https://www.facebook.com/")
    account = await get_adapter("facebook").account(page)
    assert account["external_account_id"] == "10001"
    assert page.url == "https://www.facebook.com/"
    assert requests == ["https://www.facebook.com/"]
    await context.close()


@pytest.mark.parametrize(
    "case",
    [
        "missing_cookie",
        "invalid_cookie",
        "conflicting_cookie",
        "missing_control",
        "hidden_control",
        "password",
        "checkpoint",
        "wrong_host",
    ],
)
async def test_home_does_not_accept_missing_or_conflicting_auth(browser, case):
    context = await browser.new_context()
    if case != "missing_cookie":
        await context.add_cookies(
            [
                {
                    "name": "c_user",
                    "value": "bad" if case == "invalid_cookie" else "10001",
                    "url": "https://www.facebook.com",
                }
            ]
        )
    if case == "conflicting_cookie":
        await context.add_cookies(
            [
                {
                    "name": "c_user",
                    "value": "10002",
                    "domain": ".facebook.com",
                    "path": "/",
                }
            ]
        )
    html = '<button aria-label="你的个人主页">头像</button>'
    if case == "missing_control":
        html = '<article><a href="/profile.php?id=10001">个人主页</a></article>'
    if case == "hidden_control":
        html = html.replace("<button", '<button style="display:none"')
    if case == "password":
        html += '<input type="password">'
    page = await context.new_page()
    await page.route(
        "**/*", lambda r: r.fulfill(content_type="text/html; charset=utf-8", body=html)
    )
    url = (
        "https://example.org/" if case == "wrong_host" else "https://www.facebook.com/"
    )
    if case == "checkpoint":
        url += "checkpoint/"
    await page.goto(url)
    with pytest.raises(NeedsAuth):
        await get_adapter("facebook").account(page)
    await context.close()
