"""Official Web K About/Link markup, with no live website or account access."""
import os

import pytest
from playwright.async_api import async_playwright

from lead_app import community_runtime

# morethanwords/tweb 2e4bbb52b6aec5e95ee32e5c2882c1f05de32200:
# peerProfile.tsx About/Link; rowTsx.tsx adds row-grid only for right content.
ACCOUNT = '100000017'
PEER = '-778899'
URL = 'https://t.me/privacy_fixture'
ABOUT = 'Privacy tools discussion. Be helpful and respectful.'


def about_row(body=ABOUT):
    from html import escape
    return ('<div class="row row-clickable row-with-icon row-with-padding">'
        '<div class="row-title pre-wrap">'+escape(body)+'</div>'
        '<div class="row-subtitle">Info</div>'
        '<span class="row-icon"><span class="tgico row-icon-icon">info-icon</span></span></div>')


def fixture_html(about=ABOUT):
    return '''<!doctype html><style>
    [hidden]{display:none!important}.chatlist{width:100px;height:40px}
    #column-right{position:fixed;right:0;top:0;width:330px}
    .pre-wrap{white-space:pre-wrap}.row{margin:10px}
    </style><body class="has-chat is-right-column-shown">
    <div class="chatlist">Chats</div>
    <div class="chat active" data-type="chat">
    <div class="chat-info"><span class="peer-title" data-peer-id="-778899">Privacy fixture</span>
    <span class="bottom">12 members</span></div>
    <button>Join Group</button>
    <div class="bubble" data-mid="100"><div class="message">A real fixture question?</div></div>
    </div><div id="column-right"><div class="profile-container active">
    <div class="sidebar-header"><button class="sidebar-close-button">Close</button></div>
    ABOUT_ROW
    <div class="row row-grid row-clickable row-with-icon">
    <div class="row-title">t.me/privacy_fixture</div><div class="row-subtitle">Link</div>
    <span class="row-icon">link-icon</span><span class="qr-icon">qr</span></div>
    </div></div><script>
    localStorage.setItem('account1',JSON.stringify({userId:'100000017'}));
    document.querySelector('.sidebar-close-button').onclick=()=>{
      document.querySelector('#column-right').hidden=true;
      document.body.classList.remove('is-right-column-shown');
    };
    </script></body>'''.replace('ABOUT_ROW', about_row(about) if about is not None else '')


@pytest.fixture
async def page():
    if os.environ.get('LEAD_BROWSER_TESTS') != '1':
        pytest.skip('Explicit isolated local browser fixtures only')
    async with async_playwright() as manager:
        browser = await manager.chromium.launch(headless=True, executable_path=manager.chromium.executable_path)
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.abort())
            await page.route('https://web.telegram.org/k/**',
                lambda route: route.fulfill(body=fixture_html(), content_type='text/html'))
            await page.goto('https://web.telegram.org/k/#'+PEER)
            yield page
        finally:
            await browser.close()


async def test_actual_group_read_retains_about_instead_of_link_chrome(page):
    observed = await community_runtime._read_group(page, URL, ACCOUNT, 30, None, PEER)
    assert observed['description'] == ABOUT
    assert observed['target_id'] == PEER
    assert observed['history'][0]['mid'] == '100'
    group = community_runtime._growth_group(observed, {'canonical_url':URL})
    assert group['rules'] == ABOUT and group['rules_status'] == 'complete'
