"""Recognize the signed-in owner without navigating away from Facebook home."""

from .core import NeedsAuth, check_surface

HOSTS = {"facebook.com", "www.facebook.com"}
OWNER_CONTROLS = (
    "Your profile",
    "Account",
    "你的个人主页",
    "你的個人檔案",
    "您的个人主页",
    "你的个人资料",
    "你的個人資料",
    "账户",
    "帐户",
    "账号",
    "帐号",
    "帳戶",
    "帳號",
)


async def signed_in_id(page):
    await check_surface(page, HOSTS)
    cookies = await page.context.cookies(["https://www.facebook.com"])
    owners = {c["value"] for c in cookies if c["name"] == "c_user"}
    if (
        len(owners) != 1
        or not next(iter(owners)).isascii()
        or not next(iter(owners)).isdigit()
    ):
        raise NeedsAuth("尚未检测到 Facebook 登录，请在当前窗口完成登录")
    owner = next(iter(owners))
    controls = ",".join(f'[aria-label="{label}"]' for label in OWNER_CONTROLS)
    if not await page.locator(controls).filter(visible=True).count():
        raise NeedsAuth("暂时无法识别 Facebook 当前账号，请等待页面加载完成后重试")
    await check_surface(page, HOSTS)
    return owner
