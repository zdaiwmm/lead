"""Current identity is server-derived; legacy proposal hints never select authority."""
from uuid import uuid4

import pytest
from lead_app.native_method_facts import inspect_editor
from native_browser_fixture import NativeBoard
from playwright.async_api import async_playwright


@pytest.fixture
async def editor():
    board=NativeBoard().start()
    board.state.sessions['synthetic-session']=board.state.username
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(channel="chromium",headless=True,args=[
            '--host-resolver-rules=MAP alpha.native-fixture.test 127.0.0.1',
            '--no-proxy-server'])
        context=await browser.new_context()
        async def local_only(route):
            if route.request.url.startswith(board.origin+'/'):
                return await route.continue_()
            await route.abort()
        await context.route('**/*',local_only)
        await context.add_cookies([{'name':'board_session','value':'synthetic-session',
            'domain':board.state.host,'path':'/'}])
        page=await context.new_page()
        await page.goto(board.origin+'/home')
        proposal={'target_url':board.origin+'/home','selectors':{'self_subject':'main > p',
            'editor_target':'#editor-target','editor_body':'textarea','submit':'button[type=submit]'},
            'self_subject_prefix':'Signed in as ','self_subject_suffix':' · Your account settings',
            'permission_selector':'main section p:first-of-type','permission_quote':await page.locator('main section p').inner_text()}
        kwargs={'account_id':str(uuid4()),'binding_version':3,'external_account_id':board.state.username,
                'connection_origin':board.origin,'operation':'publish','evidence_ref':'fixture-a:current-dom'}
        try:
            yield page,proposal,kwargs,board,context
        finally:
            await browser.close()
    board.close()


@pytest.mark.parametrize('selector,prefix,suffix',[
    ('#not-an-observed-current-marker','guess','wrong'),
    ('*[57]','Signed in as ',' · Your account settings'),
    ('main > p','unsupported hint','wrong suffix'),
])
async def test_server_current_identity_replaces_unused_proposal_hints(editor,selector,prefix,suffix):
    page,proposal,kwargs,board,_=editor
    proposal['selectors']['self_subject']=selector
    proposal['self_subject_prefix']=prefix
    proposal['self_subject_suffix']=suffix
    before=board.oracle()['requests'][:]
    method=await inspect_editor(page,proposal,**kwargs)
    assert method['selectors']['self_subject']!=selector or selector=='main > p'
    node=page.locator(method['selectors']['self_subject'])
    assert await node.inner_text()==method['self_subject_prefix']+kwargs['external_account_id']+method['self_subject_suffix']
    assert method['request']['url']==board.origin+'/posts'
    assert board.oracle()['requests']==before


@pytest.mark.parametrize('change',('wrong_subject','quote_only','duplicate_current'))
async def test_hints_cannot_replace_actual_unique_current_identity(editor,change):
    page,proposal,kwargs,_,_=editor
    if change=='wrong_subject':
        await page.locator('main > p').evaluate("node=>node.firstChild.textContent='Signed in as another-member · '")
    elif change=='quote_only':
        await page.locator('main > p').evaluate("node=>{const article=document.createElement('article');node.replaceWith(article);article.append(node)}")
    else:
        await page.locator('main > p').evaluate("node=>node.after(node.cloneNode(true))")
    with pytest.raises(ValueError):
        await inspect_editor(page,proposal,**kwargs)
