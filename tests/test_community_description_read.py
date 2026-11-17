"""Official Web K About/Link markup, with no live website or account access."""
import os

import pytest
from lead_app import community_runtime
from playwright.async_api import async_playwright

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
    .pre-wrap{white-space:pre-wrap;min-height:16px}.row{margin:10px}
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
    assert observed['description_read_status'] == 'complete'
    assert observed['description_read_reason'] is None
    assert observed['target_id'] == PEER
    assert observed['history'][0]['mid'] == '100'
    group = community_runtime._growth_group(observed, {'canonical_url':URL})
    assert group['rules'] == ABOUT and group['rules_status'] == 'complete'


async def test_about_waits_for_its_own_delayed_body_after_link_is_visible(page):
    await page.locator('.row-title.pre-wrap').evaluate('(e) => e.parentElement.remove()')
    await page.evaluate('''row => setTimeout(() => {
        document.querySelector('.profile-container.active').insertAdjacentHTML('beforeend', row);
        window.aboutLoaded = true;
    }, 120)''', about_row())
    observed = await community_runtime._read_group(page, URL, ACCOUNT, 30, None, PEER)
    assert await page.evaluate('window.aboutLoaded') is True
    assert observed['description'] == ABOUT


async def test_about_excludes_other_profiles_hidden_rows_members_and_user_warning(page):
    await page.locator('.profile-container.active').evaluate('''(panel, row) => {
        panel.insertAdjacentHTML('beforeend',
            '<div hidden>'+row+'</div>'+
            '<div class="row row-grid chatlist-chat"><div class="row-title pre-wrap">Member identity</div></div>'+
            '<div class="row profile-unofficial-warning"><div class="row-title pre-wrap">User warning</div></div>'+
            '<div class="row row-with-icon"><div class="row-title">Bot list</div><div class="row-subtitle">Online</div></div>');
        panel.insertAdjacentHTML('afterend', '<div class="profile-container">'+row+'</div>');
    }''', about_row('Unrelated private profile information.'))
    observed = await community_runtime._read_group(page, URL, ACCOUNT, 30, None, PEER)
    assert observed['description'] == ABOUT
    assert all(text not in observed['description'] for text in ('Member', 'warning', 'Bot', 'Unrelated'))


@pytest.mark.parametrize('case,reason',[
    ('missing','not_observed'), ('ambiguous','ambiguous'), ('empty','empty'), ('oversize','truncated'),
])
async def test_unresolved_about_has_explicit_reason_and_cannot_complete_rules(page, case, reason):
    body = page.locator('.row-title.pre-wrap')
    if case == 'missing':
        await body.evaluate('(e) => e.parentElement.remove()')
    elif case == 'ambiguous':
        await page.locator('.profile-container.active').evaluate(
            '(e, row) => e.insertAdjacentHTML("beforeend", row)', about_row('Another About body.'))
    else:
        await body.evaluate('(e, text) => e.textContent=text', '   ' if case == 'empty' else ABOUT*40)
    text, actual_reason = await community_runtime._read_description(
        page.locator('.profile-container.active'), timeout=50)
    assert actual_reason == reason
    assert len(text) <= 1800
    assert text == ''
    group = community_runtime._growth_group({'description':text,
        'description_read_status':'unresolved', 'description_read_reason':reason,
        'target_id':PEER, 'title':'Privacy fixture', 'kind':'discussion',
        'joined':False, 'can_reply':False, 'history':[]}, {'canonical_url':URL})
    assert group['rules_status'] == group['description_read_status'] == 'unresolved'
    assert group['description_read_reason'] == reason
    assert group['unread_rule_sources'] == ['public_description_'+reason]


@pytest.mark.parametrize('change', ['account', 'peer', 'handle', 'profile'])
async def test_actual_group_read_rechecks_identity_after_about_access(page, monkeypatch, change):
    read = community_runtime._read_description

    async def read_then_switch(profile):
        result = await read(profile)
        await page.evaluate('''change => {
            window.identityChangedDuringRead = true;
            if(change === 'account') localStorage.setItem('account1', JSON.stringify({userId:'100000018'}));
            if(change === 'peer') document.querySelector('.chat-info .peer-title').dataset.peerId='-999999';
            if(change === 'handle') document.querySelector('.row-grid .row-title').textContent='t.me/other_fixture';
            if(change === 'profile') document.querySelector('.profile-container.active').classList.remove('active');
        }''', change)
        return result

    monkeypatch.setattr(community_runtime, '_read_description', read_then_switch)
    with pytest.raises(ValueError, match='community_source_binding_invalid'):
        await community_runtime._read_group(page, URL, ACCOUNT, 30, None, PEER)
    assert await page.evaluate('window.identityChangedDuringRead') is True
    assert await page.locator('#column-right').is_visible()


@pytest.mark.parametrize('description,status,automation_forbidden', [
    (ABOUT+' No bots or automated messages.', 'complete', True),
    (ABOUT+' Read the pinned rules before participating.', 'unresolved', False),
    (ABOUT+' Read rules at https://example.test/rules before participating.', 'unresolved', False),
])
async def test_actual_about_read_preserves_automation_and_unread_rule_restrictions(
    page, description, status, automation_forbidden
):
    await page.locator('.row-title.pre-wrap').evaluate('(e, text) => e.textContent=text', description)
    observed = await community_runtime._read_group(page, URL, ACCOUNT, 30, None, PEER)
    group = community_runtime._growth_group(observed, {'canonical_url':URL})
    assert group['rules'] == description and group['rules_status'] == status
    assert group['automation_forbidden'] is automation_forbidden
