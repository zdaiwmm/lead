import os

import pytest
from lead_app.adapters import get_adapter
from lead_app.adapters.core import NeedsAuth
from test_adapters import context as context

pytestmark = pytest.mark.skipif(
    os.getenv("LEAD_BROWSER_TESTS") != "1", reason="requires Camoufox"
)


@pytest.mark.parametrize(
    "bad", ["", "conflict", "wrong_host", "wrong_label", "logged_out"]
)
async def test_authenticated_drawer_only(context, bad):
    _, actor = context
    page = actor.page
    own = '<a href="/user/first_owner/">查看个人资料 <span>u/first_owner</span></a>'
    if bad == "conflict":
        own += '<a href="/user/second_owner/">查看个人资料 u/second_owner</a>'
    elif bad == "wrong_host":
        own = own.replace('href="/user', 'href="https://example.org/user')
    elif bad == "wrong_label":
        own = own.replace("u/first_owner", "u/another_owner")
    menu = f"""<header><button id="expand-user-drawer-button" onclick="openDrawer()">头像</button></header>
        <div id="user-drawer-content" hidden>{own}</div>
        <script>const drawer=document.querySelector('#user-drawer-content');
        function openDrawer(){{setTimeout(()=>drawer.hidden=false,200)}}
        onkeydown=e=>{{if(e.key==='Escape')drawer.hidden=true}}</script>"""
    markup = (
        '<article><header><a href="/user/post_author">Profile</a></header></article>'
    )
    if bad != "logged_out":
        markup += menu
    await page.route(
        "https://www.reddit.com/**",
        lambda r: r.fulfill(content_type="text/html", body=markup),
    )
    await page.goto("https://www.reddit.com/user/visited_person/")
    adapter = get_adapter("reddit")
    if bad:
        with pytest.raises(NeedsAuth):
            await adapter.account(page)
        return
    assert (await adapter.account(page))["external_account_id"] == "first_owner"
    assert await page.locator("#user-drawer-content").is_hidden()
    await page.locator("#expand-user-drawer-button").click()
    await page.locator("#user-drawer-content").wait_for()
    assert (await adapter.account(page))["external_account_id"] == "first_owner"
    assert await page.locator("#user-drawer-content").is_visible()
