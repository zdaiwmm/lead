"""Dedicated person-only sign-in surfaces stop before the next model call."""
import os

import pytest
import pytest_asyncio
from lead_app.native_connection import NativeConnection
from playwright.async_api import async_playwright

pytestmark = pytest.mark.skipif(
    os.environ.get('LEAD_BROWSER_TESTS') != '1',
    reason='Explicit installed browser test runtime required',
)


@pytest_asyncio.fixture
async def connection_page():
    async with async_playwright() as manager:
        browser = await manager.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route('**/*', lambda route: route.fulfill(body='<main></main>', content_type='text/html'))
        await page.goto('https://account.example.test/login')
        controller = NativeConnection.__new__(NativeConnection)
        controller.page = page
        try:
            yield controller, page
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('html', [
    '<main><h1>Scan QR code to sign in</h1><img alt="Sign-in QR code" src="data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20width=%2280%22%20height=%2280%22/%3E"><button>Refresh code</button></main>',
    '<main><h1>Use your passkey</h1><p>Confirm this sign-in with your device.</p><button>Use security key</button></main>',
    '<main><h1>Check your phone</h1><p>Approve the sign-in on your trusted device.</p><button>Try another way</button></main>',
    '<main><h1>扫码登录</h1><img alt="登录二维码" src="data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20width=%2280%22%20height=%2280%22/%3E"><button>刷新二维码</button></main>',
    '<main><h1>请使用通行密钥</h1><p>在你的设备上确认此次登录。</p><button>使用安全密钥</button></main>',
], ids=['qr_en', 'passkey_en', 'device_en', 'qr_zh', 'passkey_zh'])
async def test_dedicated_current_person_step_is_not_an_automatic_form(connection_page, html):
    controller, page = connection_page
    await page.set_content(html)
    assert await controller.person_only_challenge()


@pytest.mark.asyncio
@pytest.mark.parametrize('html', [
    '<main><article><h1>Use your passkey</h1><p>A guide to scanning QR codes.</p></article></main>',
    '<main><h1>Sign in</h1><section hidden><h2>Scan QR code to sign in</h2><img alt="Sign-in QR code"></section><form method=post><label>Username<input name=username></label><label>Password<input name=password type=password></label><button>Sign in</button><button type=button>Use a passkey instead</button></form></main>',
    '<main><h1>Sign in</h1><p>Our members discuss passkeys and QR codes.</p><form method=post><input name=username><input name=password type=password><button>Sign in</button></form></main>',
], ids=['article', 'hidden_and_optional', 'mention_only'])
async def test_mentioned_hidden_or_optional_person_step_does_not_block_password_login(connection_page, html):
    controller, page = connection_page
    await page.set_content(html)
    assert not await controller.person_only_challenge()
