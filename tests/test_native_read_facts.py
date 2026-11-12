"""Real Chromium form observations; no database, model, grants or submitted writes."""
import pytest
from lead_app.native_read_facts import inspect_search_form
from native_browser_boundary_fixture import BoundarySite
from playwright.async_api import async_playwright

SOURCE = 'http://forms.native-read.test/current-document'
SEARCH = '''<form method="post" action="/unusual-endpoint?view=members">
<input type="hidden" name="community_id" value="community-7">
<input type="hidden" name="csrf_token" value="synthetic-csrf-secret">
<input type="hidden" name="nonce" value="synthetic-nonce-secret">
<label>Search text<input name="query" maxlength="350" value="private-current-query"></label>
<button>Search board</button></form>'''


@pytest.fixture
async def dom():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel='chromium')
        context = await browser.new_context()
        requests = []

        async def local_only(route):
            requests.append((route.request.method, route.request.url))
            await route.fulfill(body=SEARCH, content_type='text/html; charset=utf-8')

        await context.route('**/*', local_only)
        page = await context.new_page()
        page.set_default_timeout(5000)
        await page.goto(SOURCE)
        try:
            yield page, requests
        finally:
            await browser.close()


async def test_search_returns_exact_current_fields_without_query_or_nonce_values(dom):
    page, requests = dom
    before = requests[:]
    result = await inspect_search_form(page)
    assert result == {
        'source_url': SOURCE,
        'request': {'method': 'POST', 'url': 'http://forms.native-read.test/unusual-endpoint?view=members',
                    'body_kind': 'form', 'query_field': 'query',
                    'fixed_fields': {'community_id': 'community-7'},
                    'dynamic_fields': ['csrf_token', 'nonce']},
        'max_query_length': 350,
        'evidence': {'search_quote': 'Search text'},
    }
    assert not any(secret in str(result) for secret in (
        'synthetic-csrf-secret', 'synthetic-nonce-secret', 'private-current-query'))
    assert requests == before and page.url == SOURCE


async def test_search_beside_publish_form_is_not_a_publish_envelope(dom):
    page, _ = dom
    await page.set_content(SEARCH + '''<form method="post" action="/another-endpoint">
        <label>Your post<textarea name="body"></textarea></label><button>Publish post</button></form>''')
    result = await inspect_search_form(page, request_url='http://forms.native-read.test/unusual-endpoint?view=members')
    assert result['request']['query_field'] == 'query'
    with pytest.raises(ValueError):
        await inspect_search_form(page, request_url='http://forms.native-read.test/another-endpoint')
    with pytest.raises(ValueError):
        await inspect_search_form(page, request_url='http://forms.native-read.test/unusual-endpoint?view=other')


@pytest.mark.parametrize('markup,quote', [
    ('<form method=post action=/opaque role=search><input name=s><button>Go</button></form>', 'search'),
    ('<form method=post action=/opaque><input name=s role=searchbox><button>Go</button></form>', 'searchbox'),
    ('<form method=post action=/opaque><label>查询<input name=s></label><button>执行</button></form>', '查询'),
    ('<form method=post action=/opaque><input name=s aria-label="Search members"><button>Go</button></form>', 'Search members'),
    ('<form method=post action=/opaque><input name=s><button>Search members</button></form>', 'Search members'),
])
async def test_actual_accessible_search_semantics_without_endpoint_whitelist(dom, markup, quote):
    page, _ = dom
    await page.set_content(markup)
    result = await inspect_search_form(page)
    assert result['request']['url'] == 'http://forms.native-read.test/opaque'
    assert result['evidence']['search_quote'] == quote
    assert result['max_query_length'] == 2000


async def test_named_submit_and_external_associated_hidden_fields_are_complete(dom):
    page, _ = dom
    await page.locator('form').evaluate("form=>{form.id='search-form'; form.querySelector('button').name='operation'; form.querySelector('button').value='search'}")
    await page.locator('body').evaluate("body=>body.insertAdjacentHTML('beforeend','<input type=hidden form=search-form name=collection value=members>')")
    result = await inspect_search_form(page)
    assert result['request']['fixed_fields'] == {
        'community_id': 'community-7', 'operation': 'search', 'collection': 'members'}


@pytest.mark.parametrize('form_target,base_target', [
    ('_blank', None), ('another-page', None), ('_parent', None), ('_top', None),
    (None, '_blank'), (None, 'another-page'),
])
async def test_effective_target_must_stay_in_assigned_page(dom, form_target, base_target):
    page, requests = dom
    if form_target is not None:
        await page.locator('form').evaluate('(form,target)=>form.target=target', form_target)
    if base_target is not None:
        await page.locator('head').evaluate(
            '(head,target)=>{const base=document.createElement("base");base.target=target;head.append(base)}',
            base_target)
    with pytest.raises(ValueError, match='target'):
        await inspect_search_form(page)
    assert all(method == 'GET' for method, _ in requests)


@pytest.mark.parametrize('base_target', [None, '_self', '_blank', 'another-page'])
async def test_explicit_self_target_overrides_base_without_changing_envelope(dom, base_target):
    page, _ = dom
    before = await inspect_search_form(page)
    await page.locator('form').evaluate('form=>form.target="_self"')
    if base_target is not None:
        await page.locator('head').evaluate(
            '(head,target)=>{const base=document.createElement("base");base.target=target;head.append(base)}',
            base_target)
    assert await inspect_search_form(page) == before


@pytest.mark.parametrize('targets,allowed', [
    (['_self', '_blank'], True), (['_blank', '_self'], False),
])
async def test_only_first_base_with_target_sets_default_destination(dom, targets, allowed):
    page, _ = dom
    await page.locator('head').evaluate('''(head,targets)=>{
        head.insertAdjacentHTML('beforeend','<base href="/">');
        for(const target of targets){const base=document.createElement('base');base.target=target;head.append(base)}
    }''', targets)
    if allowed:
        assert (await inspect_search_form(page))['source_url'] == SOURCE
    else:
        with pytest.raises(ValueError, match='target'):
            await inspect_search_form(page)


@pytest.mark.parametrize('change', [
    'form.method="get"',
    'form.enctype="multipart/form-data"',
    'form.enctype="text/plain"',
    'form.action="https://outside.invalid/search"',
    'form.action="/search#fragment"',
    'form.querySelector("input[name=query]").name=""',
    'form.querySelector("input[name=query]").name="password"',
    'form.querySelector("input[name=query]").readOnly=true',
    'form.querySelector("input[name=query]").style.display="none"',
    'form.querySelector("input[name=query]").setAttribute("autocomplete","one-time-code")',
    'form.querySelector("input[name=query]").setAttribute("dirname","direction")',
    'form.querySelector("input[name=query]").maxLength=0',
    'form.querySelector("button").setAttribute("formaction",form.action)',
    'form.querySelector("button").setAttribute("formmethod","post")',
    'form.querySelector("button").setAttribute("formenctype","application/x-www-form-urlencoded")',
    'form.querySelector("button").disabled=true',
    'form.querySelector("button").textContent="Publish search"',
    'form.insertAdjacentHTML("beforeend", "<button>Search another way</button>")',
    'form.insertAdjacentHTML("beforeend", "<input name=extra>")',
    'form.insertAdjacentHTML("beforeend", "<input type=checkbox name=confirm checked>")',
    'form.insertAdjacentHTML("beforeend", "<input type=file name=upload>")',
    'form.insertAdjacentHTML("beforeend", "<input type=password name=credential>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=query value=duplicate>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=csrf_token value=duplicate>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=access_token value=private>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=authenticity_token value=private>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=is_admin value=true>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=isAdmin value=true>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=accountId value=other-subject>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=command value=delete_all>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=destination value=delete_all>")',
    'form.insertAdjacentHTML("beforeend", "<input type=hidden name=body value=reply>")',
    'form.insertAdjacentHTML("beforeend", "<p>Update account settings</p>")',
])
async def test_unsupported_ambiguous_or_writing_form_is_a_local_gap(dom, change):
    page, requests = dom
    await page.locator('form').evaluate('form=>{' + change + '}')
    with pytest.raises(ValueError):
        await inspect_search_form(page)
    assert all(method == 'GET' for method, _ in requests)


async def test_multiple_search_forms_are_ambiguous_until_exact_action_disambiguates(dom):
    page, _ = dom
    await page.set_content(SEARCH + SEARCH.replace('/unusual-endpoint?view=members', '/other-search'))
    with pytest.raises(ValueError, match='ambiguous'):
        await inspect_search_form(page)
    result = await inspect_search_form(page, request_url='http://forms.native-read.test/other-search')
    assert result['request']['url'].endswith('/other-search')
    await page.set_content(SEARCH + SEARCH)
    with pytest.raises(ValueError, match='ambiguous'):
        await inspect_search_form(page, request_url='http://forms.native-read.test/unusual-endpoint?view=members')


async def test_search_path_or_placeholder_alone_is_not_semantic_evidence(dom):
    page, _ = dom
    await page.set_content('<form method=post action=/search><input name=query placeholder=Search><button>Continue</button></form>')
    with pytest.raises(ValueError):
        await inspect_search_form(page)


async def test_disabled_fields_do_not_join_successful_field_set_and_length_is_capped(dom):
    page, _ = dom
    await page.locator('form').evaluate('''form=>{
        form.querySelector('[name=query]').maxLength=5000;
        form.insertAdjacentHTML('beforeend','<fieldset disabled><input name=ignored><input type=hidden name=community_id value=ignored></fieldset>')
    }''')
    result = await inspect_search_form(page)
    assert result['max_query_length'] == 2000
    assert result['request']['fixed_fields'] == {'community_id': 'community-7'}


async def test_repeated_observation_uses_current_action_and_static_values(dom):
    page, _ = dom
    old = await inspect_search_form(page)
    await page.locator('form').evaluate("form=>{form.action='/changed';form.querySelector('[name=community_id]').value='community-8'}")
    new = await inspect_search_form(page)
    assert old['request'] != new['request']
    assert new['request']['url'].endswith('/changed')
    assert new['request']['fixed_fields'] == {'community_id': 'community-8'}


async def test_current_document_mutation_during_observation_is_rejected(dom, monkeypatch):
    page, _ = dom
    evaluate = page.evaluate
    calls = 0

    async def mutate_after_first_snapshot(*args, **kwargs):
        nonlocal calls
        result = await evaluate(*args, **kwargs)
        calls += 1
        if calls == 1:
            await page.locator('form').evaluate("form=>form.action='/changed-during-observation'")
        return result

    monkeypatch.setattr(page, 'evaluate', mutate_after_first_snapshot)
    with pytest.raises(ValueError, match='document_changed'):
        await inspect_search_form(page)


async def test_frozen_boundary_search_is_observed_beside_real_publish_form():
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
                page = await context.new_page()
                page.set_default_timeout(5000)
                await page.goto(site.origin() + '/login')
                await page.get_by_label('Account', exact=True).fill(site.username)
                await page.get_by_label('Password', exact=True).fill(site.password)
                await page.get_by_role('button', name='Sign in', exact=True).click()
                await page.wait_for_url(site.origin() + '/home')
                result = await inspect_search_form(page, request_url=site.origin() + '/read')
                assert result['request'] == {
                    'method': 'POST', 'url': site.origin() + '/read', 'body_kind': 'form',
                    'query_field': 'query', 'fixed_fields': {}, 'dynamic_fields': ['nonce']}
                assert result['evidence'] == {'search_quote': 'Search text'}
                counts = site.oracle()['counts']
                assert counts.get('logins') == 1
                assert all(counts.get(name, 0) == 0 for name in ('reads', 'posts', 'replies'))
            finally:
                await browser.close()
