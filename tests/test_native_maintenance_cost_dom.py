"""Real DOM maintenance classification; no clicks, payments, database or models."""
import pytest
from lead_app.native_read_roles import controls
from lead_app.native_session_facts import message_bridges
from playwright.async_api import async_playwright

SOURCE = 'http://maintenance.native-dom.test/current-document'
SCRIPT = """for(const current of document.querySelectorAll('[data-operation]')) current.addEventListener('click',()=>{
    navigator.serviceWorker.controller.postMessage({operation:current.dataset.operation,proof:current.dataset.proof});});"""
BUTTON = '<button type="button" data-operation="refresh" data-proof="synthetic-current-control-nonce">{label}</button>'


@pytest.fixture
async def dom():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel='chromium')
        context = await browser.new_context()
        requests = []

        async def local_only(route):
            requests.append((route.request.method, route.request.url))
            if route.request.url != SOURCE:
                return await route.abort()
            await route.fulfill(body='<main></main>', content_type='text/html; charset=utf-8')

        await context.route('**/*', local_only)
        page = await context.new_page()
        await page.goto(SOURCE)
        try:
            yield page, requests
        finally:
            await browser.close()


@pytest.mark.parametrize('markup', [
    '<section>' + BUTTON.format(label='Renew membership') + '</section>',
    '<section><h2>会员续费</h2><p>Charge $50 payment for renewal.</p>'
        + BUTTON.format(label='Refresh session') + '</section>',
    '<form><p>会员续费：charge $50 payment.</p>'
        + BUTTON.format(label='Refresh membership') + '</form>',
], ids=['ambiguous-membership-renewal', 'session-in-paid-section', 'membership-in-paid-form'])
async def test_maintenance_controls_reject_payment_or_ambiguous_renewal(dom, markup):
    page, requests = dom
    await page.set_content(markup + '<script>' + SCRIPT + '</script>')
    bridge, = message_bridges(SCRIPT)
    assert await controls(page, bridge) == []
    assert requests == [('GET', SOURCE)]


async def test_plain_refresh_membership_preserves_current_bridge_message(dom):
    page, requests = dom
    await page.set_content('<section><p>Current membership status</p>'
        + BUTTON.format(label='Refresh membership') + '</section><script>' + SCRIPT + '</script>')
    bridge, = message_bridges(SCRIPT)
    assert await controls(page, bridge) == [{'message': {
        'operation': 'refresh', 'proof': 'synthetic-current-control-nonce'}}]
    assert requests == [('GET', SOURCE)]
