"""Identify the signed-in Reddit user from the account controls in their browser."""

import re
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeout

from .core import NeedsAuth

HOSTS = {"reddit.com", "www.reddit.com"}
DRAWER = '#user-drawer-content, faceplate-tracker[source="user_drawer"]'
TOGGLE = (
    '#expand-user-drawer-button, button[aria-controls="user-drawer-content"], '
    'button[aria-label="Open profile menu"], button[aria-label="打开个人资料菜单"]'
)
LEGACY_PROFILE_LABEL = re.compile(
    r"^(?:Profile|View profile|个人资料|查看个人资料)$", re.I
)


async def _owner(links, *, check_menu_label):
    owners = {}
    for link in await links.filter(visible=True).all():
        # Read just this account control. The DOM resolves relative and absolute links.
        href, label = await link.evaluate("a => [a.href, a.innerText]")
        url = urlsplit(href)
        match = re.fullmatch(r"/(?:user|u)/([A-Za-z0-9_-]+)/?", url.path)
        if not match:
            continue
        if url.scheme != "https" or url.netloc.lower() not in HOSTS:
            raise NeedsAuth("个人资料链接不属于 Reddit")
        username = match[1]
        if check_menu_label:
            labels = re.findall(r"(?<!\w)u/([A-Za-z0-9_-]+)", label, re.I)
            if not labels:
                continue
            if {name.lower() for name in labels} != {username.lower()}:
                raise NeedsAuth("Reddit 账号菜单中的用户名与个人资料链接不一致")
        owners[username.lower()] = username
    if len(owners) != 1:
        raise NeedsAuth("请在 Reddit 右上角打开头像菜单，确认显示当前账号的个人资料")
    return next(iter(owners.values()))


async def signed_in_username(page):
    # Reddit portals the drawer outside the page header. Never use a feed author
    # or the profile being visited as evidence of the current login.
    drawer = page.locator(DRAWER)
    links = drawer.locator('a[href*="/user/"], a[href*="/u/"]')
    toggle = page.locator(TOGGLE).filter(visible=True)
    opened_here = False
    try:
        if (
            not await links.filter(visible=True).count()
            and not await drawer.filter(visible=True).count()
        ):
            count = await toggle.count()
            if count > 1:
                raise NeedsAuth("无法唯一核对 Reddit 账号菜单")
            if not count:
                banner = page.get_by_role("banner").or_(page.locator("reddit-header"))
                return await _owner(
                    banner.get_by_role("link", name=LEGACY_PROFILE_LABEL),
                    check_menu_label=False,
                )
            if await toggle.get_attribute("aria-expanded") != "true":
                await toggle.click(timeout=4000)
                opened_here = True
        try:
            await links.filter(visible=True).first.wait_for(timeout=8000)
        except PlaywrightTimeout:
            raise NeedsAuth("Reddit 账号菜单未显示个人资料，请稍后重新验证") from None
        return await _owner(links, check_menu_label=True)
    finally:
        if opened_here and await drawer.filter(visible=True).count():
            await page.keyboard.press("Escape")
