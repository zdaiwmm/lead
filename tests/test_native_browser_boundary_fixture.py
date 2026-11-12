"""Fixture characterization only: these are not product authorization tests."""
import http.client
import importlib.util
from urllib.parse import urlencode, urlsplit

import pytest


def fixture_class():
    assert importlib.util.find_spec('native_browser_boundary_fixture'), 'independent boundary site is missing'
    from native_browser_boundary_fixture import BoundarySite
    return BoundarySite


def request(site, path, *, role='main', data=None, cookie=None):
    connection = http.client.HTTPConnection('127.0.0.1', site.server.server_port, timeout=5)
    headers = {'Host': urlsplit(site.origin(role)).netloc}
    if cookie:
        headers['Cookie'] = cookie
    body = None if data is None else urlencode(data)
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    connection.request('GET' if data is None else 'POST', path, body, headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode()
    connection.close()
    return result


def sign_in(site):
    status, headers, _ = request(site, '/session', data={
        'username': site.username, 'password': site.password, 'nonce': 'login-http'})
    assert status == 303
    assert '; HttpOnly;' in headers['Set-Cookie']
    return headers['Set-Cookie'].split(';', 1)[0]


def test_http_cookie_post_read_and_every_publish_reply_is_non_idempotent():
    # Removing cookie enforcement, POST reads, or adding replay dedup must fail.
    with fixture_class()() as site:
        assert request(site, '/account')[0] == 401
        cookie = sign_in(site)
        assert request(site, '/account', cookie=cookie)[0] == 200
        status, _, body = request(site, '/read', data={'query': 'welcome', 'nonce': 'read-http'}, cookie=cookie)
        assert status == 200 and 'Search results' in body
        assert site.oracle()['counts'].get('posts', 0) == 0
        for number in (1, 2):
            status, headers, _ = request(site, '/posts', cookie=cookie, data={'body': 'A real post', 'nonce': 'same-post'})
            assert (status, headers['Location']) == (303, '/posts/' + str(number))
        for number in (3, 4):
            status, headers, _ = request(site, '/replies', cookie=cookie, data={
                'body': 'A real reply', 'parent': '1', 'nonce': 'same-reply'})
            assert (status, headers['Location']) == (303, '/posts/' + str(number))
        body = request(site, '/posts/1', cookie=cookie)[2]
        assert 'A real post' in body and 'A real reply' in body
        snapshot = site.oracle()
        assert snapshot['counts']['posts'] == 2 and snapshot['counts']['replies'] == 2
        assert snapshot['counts']['reads'] == 1
        assert site.password not in repr(snapshot)
        assert all({'method', 'host', 'path', 'time', 'nonce'} <= set(row) for row in snapshot['requests'])
        assert [r['nonce'] for r in snapshot['requests'] if r['path'] == '/posts'] == ['same-post', 'same-post']
        for path in ('/oracle', '/__oracle', '/state'):
            assert request(site, path)[0] == 404
        snapshot['posts'].clear()
        assert len(site.oracle()['posts']) == 4  # the oracle is an immutable snapshot to its consumer


@pytest.fixture
def chromium_site():
    playwright = pytest.importorskip('playwright.sync_api')
    with fixture_class()() as site, playwright.sync_playwright() as manager:
        # Full Chromium honors the explicit HTTP trust list; headless shell 151 does not.
        browser = manager.chromium.launch(headless=True, channel='chromium', args=site.chromium_args)
        context = browser.new_context(service_workers='allow')
        context.set_default_timeout(10000)
        print('Fixture Chromium version:', browser.version)
        try:
            yield site, browser, context
        finally:
            context.close()
            browser.close()


def browser_sign_in(site, page):
    page.goto(site.origin() + '/login')
    page.get_by_label('Account', exact=True).fill(site.username)
    page.get_by_label('Password', exact=True).fill(site.password)
    page.get_by_role('button', name='Sign in', exact=True).click()
    page.wait_for_url(site.origin() + '/home')
    page.get_by_text('Background service ready', exact=True).wait_for()


def test_chromium_real_cookie_worker_refresh_cache_post_read_and_nested_oopif(chromium_site):
    # A fake SW, same-site frames, or page-owned refresh must fail independently.
    site, browser, context = chromium_site
    cdp = browser.new_browser_cdp_session()
    cdp.send('Target.setDiscoverTargets', {'discover': True})
    workers = []
    context.on('request', lambda r: workers.append(r.service_worker) if urlsplit(r.url).path == '/session/refresh' else None)
    page = context.new_page()
    browser_sign_in(site, page)
    assert any(c['name'] == 'board_session' and c['httpOnly'] for c in context.cookies(site.origin()))
    assert 'board_session' not in page.evaluate('document.cookie')
    assert page.evaluate("fetch('/sw/synthetic').then(r => r.text())") == 'Background synthetic note'
    cached_before = sum(r['path'] == '/cache/rules' for r in site.oracle()['requests'])
    assert page.evaluate("fetch('/cache/rules').then(r => r.text())") == 'Members may post and reply.'
    assert sum(r['path'] == '/cache/rules' for r in site.oracle()['requests']) == cached_before == 1
    assert not any(r['path'] == '/sw/synthetic' for r in site.oracle()['requests'])
    page.get_by_role('button', name='Refresh membership').click()
    page.get_by_text('Membership refreshed', exact=True).wait_for()
    assert site.oracle()['counts']['refreshes'] == 1
    assert len(workers) == 1 and workers[0] is not None
    outer = page.frame_locator('iframe')
    outer.get_by_role('heading', name='Visitor frame').wait_for()
    outer.frame_locator('iframe').get_by_role('heading', name='Identity frame').wait_for()
    targets = cdp.send('Target.getTargets')['targetInfos']
    iframe_urls = {t['url'] for t in targets if t['type'] == 'iframe'}
    assert site.origin('external') + '/frame' in iframe_urls
    assert site.origin('identity') + '/frame' in iframe_urls
    identity_frame = next(f for f in page.frames if f.url == site.origin('identity') + '/frame')
    assert identity_frame.parent_frame.url == site.origin('external') + '/frame'
    assert all(t['browserContextId'] == next(t['browserContextId'] for t in targets if t['type'] == 'page' and t['url'] == page.url)
               for t in targets if t['type'] == 'iframe')
    outer.get_by_label('Visitor note').fill('Cross-site side effect')
    outer.get_by_role('button', name='Send visitor note').click()
    outer.get_by_text('Visitor note received', exact=True).wait_for()
    assert site.oracle()['counts']['external_writes'] == 1
    page.get_by_label('Search text').fill('welcome')
    page.get_by_role('button', name='Search board').click()
    page.get_by_role('heading', name='Search results').wait_for()
    assert site.oracle()['counts']['reads'] == 1 and not site.oracle()['posts']
    page.goto(site.origin() + '/home')
    page.get_by_label('Your post', exact=True).fill('Chromium original')
    page.get_by_role('button', name='Publish post', exact=True).click()
    page.wait_for_url(site.origin() + '/posts/1')
    page.get_by_label('Your reply', exact=True).fill('Chromium reply')
    page.get_by_role('button', name='Publish reply', exact=True).click()
    page.wait_for_url(site.origin() + '/posts/2')
    page.goto(site.origin() + '/posts/1')
    assert page.get_by_text('Chromium original', exact=True).is_visible()
    assert page.get_by_text('Chromium reply', exact=True).is_visible()
    assert site.oracle()['counts']['posts'] == site.oracle()['counts']['replies'] == 1
    cdp.detach()


def test_chromium_sso_popup_returns_to_main_wrong_opener_and_auth_endpoint_reuse_are_triggerable(chromium_site):
    # These attempts must reach this ungated fixture, so later gate tests can demand zero origin requests.
    site, _, context = chromium_site
    for role in ('main', 'external'):
        opener = context.new_page()
        opener.goto(site.origin(role) + ('/home' if role == 'main' else '/visitor'))
        with opener.expect_popup() as opened:
            opener.get_by_role('button', name='Use identity sign-in').click()
        popup = opened.value
        popup.wait_for_url(site.origin('identity') + '/authorize?**')
        popup.get_by_label('Account', exact=True).fill(site.username)
        popup.get_by_label('Password', exact=True).fill(site.password)
        popup.get_by_role('button', name='Continue sign-in').click()
        popup.wait_for_url(site.origin() + '/session/complete?**')
        popup.get_by_role('heading', name='Sign-in complete').wait_for()
        assert popup.opener() == opener
        assert popup.context == context
        popup.close()
        opener.close()
    assert site.oracle()['counts']['sso_completions'] == 2
    page = context.new_page()
    page.goto(site.origin() + '/home')
    page.get_by_text('Background service ready', exact=True).wait_for()
    page.get_by_role('button', name='Repeat background sign-in completion').click()
    page.get_by_text('Background sign-in completion returned 200', exact=True).wait_for()
    assert site.oracle()['counts']['background_completions'] == 1
    page.get_by_label('Your post', exact=True).fill('Replay candidate')
    page.get_by_role('button', name='Publish post', exact=True).click()
    page.wait_for_url(site.origin() + '/posts/1')
    page.get_by_role('button', name='Publish another copy').click()
    page.wait_for_url(site.origin() + '/posts/2')
    snapshot = site.oracle()
    assert snapshot['counts']['posts'] == 2
    posts = [r for r in snapshot['requests'] if r['method'] == 'POST' and r['path'] == '/posts']
    assert len(posts) == 2 and posts[0]['nonce'] == posts[1]['nonce']
    assert site.password not in repr(snapshot)
