"""Finite identity facts from real Chromium DOM; no navigation by the observer."""
import pytest
from lead_app.native_identity_facts import inspect_private_subject, inspect_self_entry
from native_browser_boundary_fixture import BoundarySite
from playwright.async_api import async_playwright

SOURCE = 'http://identity.native-dom.test/current'
ENTRY = '<a id="self" rel="me" href="/private-account">Your account settings</a>'


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel='chromium')
        context = await browser.new_context()

        async def local_only(route):
            if route.request.url == SOURCE:
                return await route.fulfill(body='<main></main>', content_type='text/html; charset=utf-8')
            await route.abort()

        await context.route('**/*', local_only)
        current = await context.new_page()
        await current.goto(SOURCE)
        try:
            yield current
        finally:
            await browser.close()


async def entry(page, **changes):
    return await inspect_self_entry(page, **{
        'link_selector': '#self', 'subject_selector': '#acting',
        'subject_prefix': 'Signed in as ', 'connection_origin': SOURCE, **changes})


@pytest.mark.parametrize('markup,suffix', [
    ('<p id="acting">Signed in as member-a · ' + ENTRY + '</p>', ' · Your account settings'),
    ('<p id="acting">Signed in as member-a</p>' + ENTRY, ''),
    ('<p><span id="acting">Signed in as member-a</span> · ' + ENTRY + '</p>', ''),
    ('<nav><p id="acting">Signed in as member-a</p>' + ENTRY + '</nav>', ''),
])
async def test_current_identity_and_self_link_share_one_block_or_adjacent_sibling(page, markup, suffix):
    await page.set_content(markup)
    assert await entry(page, subject_suffix=suffix) == {
        'self_url': 'http://identity.native-dom.test/private-account',
        'self_link_selector': '#self', 'acting_subject': 'member-a'}
    assert page.url == SOURCE


async def test_posting_as_is_current_identity_semantics(page):
    await page.set_content('<p id="acting">Posting as @member-a</p>' + ENTRY)
    assert (await entry(page, subject_prefix='Posting as '))['acting_subject'] == '@member-a'


@pytest.mark.parametrize('markup', [
    '<article><p id="acting">Signed in as member-a</p>' + ENTRY + '</article>',
    '<blockquote><p id="acting">Signed in as member-a</p>' + ENTRY + '</blockquote>',
    '<nav><p id="acting">member-a</p>' + ENTRY + '</nav>',
    '<p id="acting">Signed in as member-a</p><p>Signed in as member-b</p>' + ENTRY,
    '<p id="acting">Signed in as member-a member-b</p>' + ENTRY,
    '<p id="acting">Signed in as member-a</p>' + ENTRY + '<a rel="me" href="/other-private">Other self</a>',
    '<section><p id="acting">Signed in as member-a</p></section><section>' + ENTRY + '</section>',
    '<p id="acting">Signed in as member-a</p>Unrelated identity region' + ENTRY,
    '<p id="acting">Signed in as member-a</p>' + ENTRY.replace('/private-account', 'https://outside.invalid/account'),
])
async def test_public_ambiguous_or_unrelated_self_entries_are_rejected(page, markup):
    await page.set_content(markup)
    with pytest.raises(ValueError):
        await entry(page)


async def test_entry_prefix_and_connection_origin_are_observed_constraints(page):
    await page.set_content('<p id="acting">Author: member-a</p>' + ENTRY)
    with pytest.raises(ValueError):
        await entry(page, subject_prefix='Author: ')
    await page.set_content('<p id="acting">Signed in as member-a</p>' + ENTRY)
    with pytest.raises(ValueError):
        await entry(page, connection_origin='https://identity.native-dom.test')


async def test_model_suffix_cannot_turn_current_subject_into_another_identifier(page):
    await page.set_content('<p id="acting">Signed in as member-alpha</p>' + ENTRY)
    with pytest.raises(ValueError):
        await entry(page, subject_suffix='-alpha')


@pytest.mark.parametrize('markup,selector,changes,prefix,suffix,label', [
    ('<p id="value">Account ID: member-a</p>', '#value', {}, 'Account ID: ', '', 'Account ID:'),
    ('<p id="value">Account ID: member-a (private)</p>', '#value',
        {'value_prefix': 'Account ID: ', 'value_suffix': ' (private)'}, 'Account ID: ', ' (private)', 'Account ID:'),
    ('<p>Account ID: <strong id="value">member-a</strong></p>', '#value', {}, '', '', 'Account ID:'),
    pytest.param('<p><span>Account ID: </span><strong id="value">member-a</strong></p>',
        '#value', {}, '', '', 'Account ID:', id='split-label-and-value'),
    ('<span id="value" aria-label="Username">member-a</span>', '#value', {}, '', '', 'Username'),
    ('<input id="value" aria-label="Handle" readonly value="member-a">', '#value', {}, '', '', 'Handle'),
    ('<input id="value" aria-label="Username" disabled value="member-a">', '#value', {}, '', '', 'Username'),
    ('<dl><dt>Username</dt><dd id="value">member-a</dd></dl>', '#value', {}, '', '', 'Username'),
])
async def test_private_subject_returns_actual_value_and_reusable_literal_affixes(
        page, markup, selector, changes, prefix, suffix, label):
    await page.set_content(markup)
    assert await inspect_private_subject(page, subject_selector=selector, **changes) == {
        'subject': 'member-a', 'self_selector': selector, 'value_prefix': prefix, 'value_suffix': suffix,
        'evidence': {'label': label}}
    assert page.url == SOURCE


@pytest.mark.parametrize('markup', [
    '<p id="value">Account ID: member-a member-b</p>',
    '<p id="value">Account ID: <span>member-a</span><span>member-b</span></p>',
    '<article><p>Account ID: <span id="value">member-a</span></p></article>',
    '<blockquote><p id="value">Account ID: member-a</p></blockquote>',
    '<p id="value">Account ID:</p>',
    '<span id="value" title="Account ID">member-a</span>',
    pytest.param('<article><p id="public-label">Account ID</p></article><span id="value" aria-labelledby="public-label">member-a</span>', id='public-label-association'),
    '<p>Account ID: member-b</p><p>Account ID: <span id="value">member-a</span></p>',
    '<p>Account ID: member-b</p><div><span id="value">member-a</span></div>',
    '<p id="value">Account ID: {"id":"member-a"}</p>',
    '<input id="value" aria-label="Account ID" type="password" value="' + 'synthetic-secret-' * 30 + '">',
    '<input id="value" aria-label="Username" value="member-a">',
    '<input id="value" aria-label="Username" readonly autocomplete="one-time-code" value="135790">',
    '<form><h2>Sign in</h2><input id="value" aria-label="Username" readonly value="member-a"></form>',
    '<a id="value" rel="me" href="/account">Your account settings</a>',
    '<p>Account ID: <span class="value">member-a</span></p><p>Account ID: <span class="value">member-b</span></p>',
])
async def test_private_subject_rejects_multiple_public_empty_and_forged_fields(page, markup):
    await page.set_content(markup)
    selector = '.value' if 'class="value"' in markup else '#value'
    with pytest.raises(ValueError):
        await inspect_private_subject(page, subject_selector=selector)


async def test_explicit_affix_cannot_hide_another_account_value(page):
    await page.set_content('<p id="value">Account ID: member-a / Account ID: member-b</p>')
    with pytest.raises(ValueError):
        await inspect_private_subject(page, subject_selector='#value',
            value_prefix='Account ID: ', value_suffix=' / Account ID: member-b')


async def test_reusable_affix_reports_actual_changed_subject_for_caller_comparison(page):
    await page.set_content('<p id="value">Account ID: member-a</p>')
    first = await inspect_private_subject(page, subject_selector='#value')
    await page.locator('#value').evaluate("node=>node.textContent='Account ID: member-b'")
    current = await inspect_private_subject(page, subject_selector='#value',
        value_prefix=first['value_prefix'], value_suffix=first['value_suffix'])
    assert current['subject'] == 'member-b'


async def test_frozen_boundary_adjacent_entry_and_whole_private_paragraph():
    with BoundarySite() as site:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, channel='chromium', args=site.chromium_args)
            try:
                context = await browser.new_context()

                async def fixture_only(route):
                    if any(route.request.url.startswith(site.origin(role) + '/') for role in site.hosts):
                        return await route.continue_()
                    await route.abort()

                await context.route('**/*', fixture_only)
                current = await context.new_page()
                current.set_default_timeout(5000)
                await current.goto(site.origin() + '/login')
                await current.get_by_label('Account', exact=True).fill(site.username)
                await current.get_by_label('Password', exact=True).fill(site.password)
                await current.get_by_role('button', name='Sign in', exact=True).click()
                await current.wait_for_url(site.origin() + '/home')
                found = await inspect_self_entry(current, link_selector='a[rel~=me]',
                    subject_selector='main > p:first-of-type', subject_prefix='Signed in as ',
                    connection_origin=site.origin())
                assert found['acting_subject'] == site.username
                await current.goto(found['self_url'])
                private = await inspect_private_subject(current, subject_selector='main > p')
                assert private['subject'] == site.username
                assert private['value_prefix'] == 'Account ID: '
                assert site.oracle()['counts'].get('logins') == 1
            finally:
                await browser.close()
