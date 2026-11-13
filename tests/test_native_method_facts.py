"""Real Chromium DOM facts from the frozen A fixture; no model, account or database writes."""
from copy import deepcopy
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
        browser=await playwright.chromium.launch(headless=True,args=[
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


async def test_publish_facts_are_current_form_not_model_recipe(editor):
    page,proposal,kwargs,board,_=editor
    before=board.oracle()['requests'][:]
    method=await inspect_editor(page,proposal,**kwargs)
    assert method['request']=={'method':'POST','url':board.origin+'/posts','body_kind':'form',
        'content_field':'body','fixed_fields':{},'dynamic_fields':[]}
    assert method['role']=='ordinary_member' and method['operations']==['publish']
    assert method['receipt_state']=='unobserved'
    assert not any(key.startswith('receipt_') for key in method['selectors'])
    assert method['account_id']==kwargs['account_id'] and method['binding_version']==3
    assert method['platform_permission']['source_url']==proposal['target_url']
    assert board.oracle()['requests']==before


async def test_reply_form_reads_current_parent_and_separate_current_rule_page(editor):
    page,proposal,kwargs,board,context=editor
    board.state.posts.append({'id':1,'author':'synthetic-other-member','body':'Original fixture question','parent_url':None})
    permission=await context.new_page()
    await permission.goto(proposal['target_url'])
    await page.goto(board.origin+'/posts/1')
    proposal['selectors']['editor_parent']='#editor-parent'
    kwargs['operation']='reply'
    method=await inspect_editor(page,proposal,permission_page=permission,**kwargs)
    assert method['request']=={'method':'POST','url':board.origin+'/replies','body_kind':'form',
        'content_field':'body','parent_field':'parent_url','fixed_fields':{},'dynamic_fields':[]}
    assert method['operations']==['reply'] and method['receipt_state']=='unobserved'
    assert method['platform_permission']['source_url']==permission.url
    assert method['self_url']==page.url
    assert not any(item['method']=='POST' for item in board.oracle()['requests'])


async def test_hidden_fields_are_frozen_and_only_csrf_nonce_are_dynamic(editor):
    page,proposal,kwargs,_,_=editor
    await page.locator('form').evaluate('''form=>{for(const [name,value] of [['board_id','community-7'],['csrf_token','private-csrf'],['nonce','private-nonce']]){
      const input=document.createElement('input');input.type='hidden';input.name=name;input.value=value;form.append(input)}}''')
    method=await inspect_editor(page,proposal,**kwargs)
    assert method['request']['fixed_fields']=={'board_id':'community-7'}
    assert method['request']['dynamic_fields']==['csrf_token','nonce']
    assert 'private-csrf' not in str(method) and 'private-nonce' not in str(method)


@pytest.mark.parametrize('change',[
    'form.method="get"',
    'form.enctype="multipart/form-data"',
    'form.querySelector("textarea").name=""',
    'form.querySelector("button").setAttribute("formaction","https://outside.invalid/send")',
    '''const input=document.createElement('input');input.type='text';input.name='extra_write';form.append(input)''',
    '''for(let i=0;i<2;i++){const input=document.createElement('input');input.type='hidden';input.name='duplicate';input.value='x';form.append(input)}''',
    '''const input=document.createElement('input');input.type='hidden';input.name='is_admin';input.value='true';form.append(input)''',
    '''const input=document.createElement('input');input.type='hidden';input.name='access_token';input.value='secret';form.append(input)''',
    '''const input=document.createElement('input');input.type='hidden';input.name='authenticity_token';input.value='private';form.append(input)''',
    '''const input=document.createElement('input');input.type='hidden';input.name='command';input.value='delete_all';form.append(input)''',
])
async def test_unsupported_or_privileged_forms_are_local_gaps(editor,change):
    page,proposal,kwargs,_,_=editor
    await page.locator('form').evaluate('form=>{'+change+'}')
    with pytest.raises(ValueError):
        await inspect_editor(page,proposal,**kwargs)


async def test_wrong_current_account_or_target_or_origin_is_rejected(editor):
    page,proposal,kwargs,board,_=editor
    with pytest.raises(ValueError,match='identity'):
        await inspect_editor(page,proposal,**{**kwargs,'external_account_id':'different-member'})
    with pytest.raises(ValueError,match='origin'):
        await inspect_editor(page,proposal,**{**kwargs,'connection_origin':'https://alpha.native-fixture.test'})
    await page.locator('#editor-target').evaluate("el=>el.href='/some-other-board'")
    with pytest.raises(ValueError,match='target'):
        await inspect_editor(page,proposal,**kwargs)
    assert not any(item['method']=='POST' for item in board.oracle()['requests'])


@pytest.mark.parametrize('prohibition',[
    'Automated browser tools are prohibited for members.',
    'However, automation is not allowed.',
    'Only administrators may publish or reply.',
    'Members cannot reply to posts.',
    '禁止普通成员使用自动工具发布或回复。',
])
async def test_matching_positive_quote_cannot_hide_current_contradiction(editor,prohibition):
    page,proposal,kwargs,_,_=editor
    await page.locator('section').evaluate("(section,text)=>{const p=document.createElement('p');p.textContent=text;section.append(p)}",prohibition)
    with pytest.raises(ValueError,match='permission'):
        await inspect_editor(page,proposal,**kwargs)


async def test_quote_must_be_from_the_current_observed_rule_element(editor):
    page,proposal,kwargs,_,_=editor
    proposal['permission_quote']='The model asserts that automated publishing is permitted.'
    with pytest.raises(ValueError,match='permission'):
        await inspect_editor(page,proposal,**kwargs)


async def test_identity_in_an_article_is_not_current_account_evidence(editor):
    page,proposal,kwargs,_,_=editor
    await page.locator('main').evaluate("main=>{main.querySelector(':scope > p').remove();const article=document.createElement('article');article.innerHTML='<p>Signed in as fixture-member</p>';main.append(article)}")
    proposal['selectors']['self_subject']='article p'
    proposal['self_subject_suffix']=''
    with pytest.raises(ValueError,match='identity'):
        await inspect_editor(page,proposal,**kwargs)


async def test_reply_hidden_parent_must_match_current_original_post(editor):
    page,proposal,kwargs,board,context=editor
    board.state.posts.append({'id':1,'author':'synthetic-other-member','body':'Original fixture question','parent_url':None})
    permission=await context.new_page()
    await permission.goto(proposal['target_url'])
    await page.goto(board.origin+'/posts/1')
    proposal['selectors']['editor_parent']='#editor-parent'
    kwargs['operation']='reply'
    await page.locator('input[name=parent_url]').evaluate("el=>el.value=location.origin+'/posts/999'")
    with pytest.raises(ValueError,match='parent'):
        await inspect_editor(page,proposal,permission_page=permission,**kwargs)
    await page.locator('input[name=parent_url]').evaluate("el=>el.value=location.href")
    await page.locator('#editor-parent').evaluate("el=>el.href=location.origin+'/posts/999'")
    with pytest.raises(ValueError,match='parent'):
        await inspect_editor(page,proposal,permission_page=permission,**kwargs)


async def test_unrelated_permission_page_and_ambiguous_selectors_are_rejected(editor):
    page,proposal,kwargs,board,context=editor
    permission=await context.new_page()
    await permission.goto(board.origin+'/account')
    with pytest.raises(ValueError,match='permission'):
        await inspect_editor(page,proposal,permission_page=permission,**kwargs)
    ambiguous=deepcopy(proposal)
    ambiguous['selectors']['editor_body']='p'
    with pytest.raises(ValueError):
        await inspect_editor(page,ambiguous,**kwargs)


async def test_submit_must_belong_to_the_observed_body_form(editor):
    page,proposal,kwargs,_,_=editor
    await page.locator('main').evaluate("main=>{const form=document.createElement('form');form.method='post';form.action='/another-operation';form.innerHTML='<button id=other-submit type=submit>Continue</button>';main.append(form)}")
    proposal['selectors']['submit']='#other-submit'
    with pytest.raises(ValueError,match='form_unsupported'):
        await inspect_editor(page,proposal,**kwargs)
