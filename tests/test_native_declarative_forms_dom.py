"""Independent common HTML contracts; no site adapter, model, DB or submission."""
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
import pytest_asyncio
from lead_app.browser_execution import match_request, validate_method
from lead_app.native_action import NativeAction
from lead_app.native_method_facts import inspect_editor
from playwright.async_api import async_playwright

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',
    reason='Explicit isolated common HTML verification required')]
ORIGIN='https://member.example.test'
RULES='You are an ordinary member. Members may publish and reply. Automated browser tools are permitted.'


@pytest_asyncio.fixture
async def common_page():
    async with async_playwright() as playwright:
        asset=Path(os.environ.get('LEAD_BROWSER_RUNTIME_DIR','/missing'))/'chromium/chrome-linux64/chrome'
        options={'headless':True,'args':['--no-sandbox','--disable-background-networking']}
        if asset.is_file():
            options['executable_path']=str(asset)
        browser=await playwright.chromium.launch(**options)
        context=await browser.new_context(service_workers='block')
        requests=[]
        async def route(current):
            requests.append(current.request.method)
            await current.fulfill(body='<main></main>',content_type='text/html')
        await context.route('**/*',route)
        page=await context.new_page()
        await page.goto(ORIGIN+'/records/42')
        try:
            yield page,context,requests
        finally:
            await browser.close()


async def editor(page,context,*,canonical='/records/42',value='42',field='record_ref'):
    parent=ORIGIN+canonical
    await page.set_content('<main><p id=current>Signed in as member-A</p><a id=target href=/board>Board</a>'
        '<a id=parent rel=canonical href="'+parent+'">Original post</a><form method=post action=/respond>'
        '<input type=hidden name="'+field+'" value="'+value+'"><input type=hidden name=nonce value=private-nonce>'
        '<label>Reply<textarea id=reply-box name=message></textarea></label><button id=send>Send reply</button></form></main>')
    permission=await context.new_page()
    await permission.goto(ORIGIN+'/board')
    await permission.set_content('<section><p id=rules>'+RULES+'</p></section>')
    proposal={'target_url':ORIGIN+'/board','selectors':{'self_subject':'#current','editor_target':'#target',
        'editor_parent':'#parent','editor_body':'#reply-box','submit':'#send'},'self_subject_prefix':'Signed in as ',
        'self_subject_suffix':'','permission_selector':'#rules','permission_quote':RULES}
    kwargs={'account_id':str(uuid4()),'binding_version':1,'external_account_id':'member-A',
        'connection_origin':ORIGIN,'operation':'reply','evidence_ref':'common-current-form','permission_page':permission}
    return proposal,kwargs,parent


@pytest.mark.parametrize('canonical',['/records/42','/entry?item=42'])
async def test_single_hidden_numeric_parent_is_derived_from_actual_canonical(common_page,canonical):
    page,context,requests=common_page
    proposal,kwargs,parent=await editor(page,context,canonical=canonical)
    method=await inspect_editor(page,proposal,**kwargs)
    binding={'canonical_url':parent,'value':'42'}
    assert method['request']['parent_field']=='record_ref'
    assert method['request']['parent_binding']==binding
    assert 'private-nonce' not in str(method)
    validated=validate_method(method,account_id=kwargs['account_id'],binding_version=1,
        target_url=ORIGIN+'/board',operation='reply')
    intent={'operation':'reply','parent_url':parent,'body':'Original answer'}
    outgoing={'method':'POST','url':ORIGIN+'/respond','hasPostData':True,
        'postData':urlencode({'message':intent['body'],'record_ref':'42','nonce':'live-nonce'})}
    assert match_request(validated,outgoing,intent)
    for changed in ({**intent,'parent_url':ORIGIN+'/records/43'},):
        with pytest.raises(ValueError):
            match_request(validated,outgoing,changed)
    with pytest.raises(ValueError):
        match_request(validated,{**outgoing,'postData':outgoing['postData'].replace('record_ref=42','record_ref=43')},intent)
    invalid={**method,'request':{**method['request'],'parent_binding':{**binding,'value':'43'}}}
    with pytest.raises(ValueError):
        validate_method(invalid,account_id=kwargs['account_id'],binding_version=1,target_url=ORIGIN+'/board',operation='reply')
    for unsafe in ('accountId','author_id','role','command','amount','access_token'):
        with pytest.raises(ValueError):
            validate_method({**method,'request':{**method['request'],'parent_field':unsafe}},account_id=kwargs['account_id'],binding_version=1,target_url=ORIGIN+'/board',operation='reply')
    assert set(requests)=={'GET'}


@pytest.mark.parametrize('change',['wrong_id','two_fields','two_path_ids','two_url_ids','no_canonical','wrong_parent',
    'role_parent','credential_parent','privileged_extra'])
async def test_parent_mapping_requires_unique_safe_current_dom_evidence(common_page,change):
    page,context,requests=common_page
    proposal,kwargs,parent=await editor(page,context,
        canonical='/groups/8/records/42' if change=='two_path_ids' else '/records/42?page=2' if change=='two_url_ids' else '/records/42',
        value='43' if change=='wrong_id' else '42',field='role' if change=='role_parent' else 'auth_token' if change=='credential_parent' else 'record_ref')
    if change=='two_fields':
        await page.locator('form').evaluate("form=>form.insertAdjacentHTML('beforeend','<input type=hidden name=another_ref value=42>')")
    elif change=='no_canonical':
        await page.locator('#parent').evaluate("node=>node.removeAttribute('rel')")
    elif change=='wrong_parent':
        await page.locator('#parent').evaluate("node=>{node.href='/records/43';node.removeAttribute('rel');const c=document.createElement('link');c.rel='canonical';c.href='/records/42';document.head.append(c)}")
    elif change=='privileged_extra':
        await page.locator('form').evaluate("form=>form.insertAdjacentHTML('beforeend','<input type=hidden name=operation value=delete_account>')")
    with pytest.raises(ValueError):
        await inspect_editor(page,proposal,**kwargs)
    assert set(requests)=={'GET'}


async def test_full_url_parent_encoding_remains_supported(common_page):
    page,context,_=common_page
    proposal,kwargs,parent=await editor(page,context,value=ORIGIN+'/records/42')
    method=await inspect_editor(page,proposal,**kwargs)
    assert method['request']['parent_field']=='record_ref' and 'parent_binding' not in method['request']
    assert match_request(method,{'method':'POST','url':ORIGIN+'/respond','postData':urlencode({'message':'Reply',
        'record_ref':parent,'nonce':'live'})},{'operation':'reply','parent_url':parent,'body':'Reply'})


@pytest.mark.parametrize('structure',['address','legacy_author','p_by','fake_address','quoted','other_article',
    'two_authors','by_after','wrong_handle','not_by'])
async def test_semantic_address_byline_belongs_to_actual_receipt_article(common_page,structure):
    page,context,_=common_page
    await page.goto(ORIGIN+'/records/900')
    byline='<address>By <a id=author href=/members/a>member-A</a></address>'
    if structure=='legacy_author':
        byline='<p>Author: <a id=author href=/members/a>member-A</a></p>'
    elif structure=='p_by':
        byline=byline.replace('address','p')
    elif structure=='fake_address':
        byline='<div role=address aria-label=By><a id=author>member-A</a></div>'
    elif structure=='quoted':
        byline='<blockquote>'+byline+'</blockquote>'
    elif structure=='two_authors':
        byline=byline.replace('</address>',' and <a href=/members/b>member-B</a></address>')
    elif structure=='by_after':
        byline='<address><a id=author>member-A</a> By</address>'
    elif structure=='wrong_handle':
        byline=byline.replace('member-A','member-B')
    elif structure=='not_by':
        byline=byline.replace('By ','Not By ')
    content='<p id=body>Original answer</p><a id=url rel=canonical href=/records/900>Canonical</a><a id=parent href=/records/42>In reply to original post</a>'
    html='<article>'+('' if structure=='other_article' else byline)+content+'</article>'
    if structure=='other_article':
        html+='<article>'+byline+'</article>'
    await page.set_content(html)
    controller=NativeAction.__new__(NativeAction)
    controller.action={'type':'reply','intent':{'body':'Original answer','parent_url':ORIGIN+'/records/42',
        'browser_method':{'target_url':ORIGIN+'/board'}}}
    controller.actor=SimpleNamespace()
    controller.snapshot=lambda:({}, {'external_account_id':'member-A'}, {})
    async def document(current):
        return current.url
    controller.document=document
    receipt=await controller.read_receipt(page,{'author':'#author','body':'#body','url':'#url','parent':'#parent'})
    assert bool(receipt) is (structure in ('address','legacy_author'))


@pytest.mark.parametrize('rule,allowed',[
    ('You are an ordinary member. Members may use authorized browser automation to publish posts and reply to messages.',True),
    ('You are an ordinary member. Members can publish posts and reply to messages, including through authorized browser automation.',True),
    ('You are an ordinary member. Members may not use authorized browser automation to publish posts or reply to messages.',False),
    ('You are an ordinary member. Only administrators may use authorized browser automation to publish and reply.',False),
    ('You are an ordinary member. Members may use authorized browser automation to read messages.',False),
    ('Members may use authorized browser automation to publish and reply.',False),
    ('Ordinary members can publish posts and reply, including through authorized browser automation.',True),
    ('You are an ordinary member. Members may use authorized browser automation to read. Members can reply manually.',False),
    ('You are an ordinary member. Members may use authorized browser automation to publish and reply if an administrator approves.',False),
])
async def test_authorized_member_automation_is_explicit_current_permission(common_page,rule,allowed):
    page,context,_=common_page
    proposal,kwargs,parent=await editor(page,context,value=ORIGIN+'/records/42')
    await kwargs['permission_page'].locator('#rules').evaluate('(node,text)=>node.innerText=text',rule)
    proposal['permission_quote']=rule
    if allowed:
        assert (await inspect_editor(page,proposal,**kwargs))['platform_permission']['state']=='approved'
    else:
        with pytest.raises(ValueError):
            await inspect_editor(page,proposal,**kwargs)


@pytest.mark.parametrize('structure',['local_nav','nav_wrapper','nav_wrapper_aria','no_current','different_block','multiple_subjects',
    'cross_origin','quoted','article','private_nav'])
async def test_only_current_local_navigation_identity_can_lead_to_private_self(common_page,structure):
    from lead_app.native_identity_facts import (
        inspect_private_subject,
        inspect_self_entry,
    )

    page,context,_=common_page
    html='<header><nav><span id=current>Signed in as <strong>member-A</strong></span><a id=self rel=me href=/account>Account</a></nav></header>'
    suffix=''
    if structure in ('nav_wrapper','nav_wrapper_aria'):
        html='<header><nav id=current>Signed in as <span>member-A</span> <a id=self rel=me href=/account>Account</a></nav></header>'
        if structure=='nav_wrapper_aria':
            html=html.replace('<nav id=current>','<nav id=current aria-label="Main navigation">')
        suffix=' Account'
    elif structure=='no_current':
        html=html.replace('Signed in as ','')
    elif structure=='different_block':
        html='<nav><div><span id=current>Signed in as member-A</span></div><div><a id=self rel=me href=/account>Account</a></div></nav>'
    elif structure=='multiple_subjects':
        html+='<p>Signed in as member-B</p>'
    elif structure=='cross_origin':
        html=html.replace('href=/account','href=https://another.example.test/account')
    elif structure in ('quoted','article'):
        tag='blockquote' if structure=='quoted' else 'article'
        html='<'+tag+'>'+html+'</'+tag+'>'
    elif structure=='private_nav':
        await page.set_content('<nav><p id=private>Account ID: member-A</p></nav>')
        with pytest.raises(ValueError):
            await inspect_private_subject(page,subject_selector='#private')
        return
    await page.set_content(html)
    args={'link_selector':'#self','subject_selector':'#current','subject_prefix':'Signed in as ',
        'subject_suffix':suffix,'connection_origin':ORIGIN}
    if structure in ('local_nav','nav_wrapper','nav_wrapper_aria'):
        assert await inspect_self_entry(page,**args)=={'self_url':ORIGIN+'/account','self_link_selector':'#self','acting_subject':'member-A'}
    else:
        with pytest.raises(ValueError):
            await inspect_self_entry(page,**args)


@pytest.mark.parametrize('selected,extra,allowed',[
    ('#name','',True),('#id','',True),
    ('#name','<dt>Username</dt><dd>member-B</dd>',False),
    ('#id','<dt>Account identifier</dt><dd>stable-other</dd>',False),
    ('#name','<article><p>Username: old-public-author</p></article>',True),
    ('#name','<dt>Handle</dt><dd>another-public-alias</dd>',True),
])
async def test_private_identity_is_unique_within_selected_explicit_field_kind(common_page,selected,extra,allowed):
    from lead_app.native_identity_facts import inspect_private_subject

    page,context,_=common_page
    await page.set_content('<main><dl><dt>Account ID</dt><dd id=id>stable-271</dd><dt>Username</dt><dd id=name>member-A</dd>'+extra+'</dl></main>')
    if allowed:
        result=await inspect_private_subject(page,subject_selector=selected)
        assert result['subject']==('member-A' if selected=='#name' else 'stable-271')
    else:
        with pytest.raises(ValueError):
            await inspect_private_subject(page,subject_selector=selected)


@pytest.mark.parametrize('location,allowed',[('article',True),('adjacent',True),('body',False),('quote',False),('navigation',False),('other_article',False)])
async def test_parent_metadata_can_use_its_single_local_paragraph_label(common_page,location,allowed):
    page,context,_=common_page
    await page.goto(ORIGIN+'/records/900')
    metadata='<p>In reply to <a id=parent href=/records/42>post 42</a></p>'
    content='<p>Author: <span id=author>member-A</span></p><div id=body>Original answer</div><a id=url href=/records/900>Canonical</a>'
    if location=='body':
        html='<article><p><span id=body>Original answer</span> In reply to <a id=parent href=/records/42>post 42</a></p><p>Author: <span id=author>member-A</span></p><a id=url href=/records/900>Canonical</a></article>'
    elif location=='article':
        html='<article>'+content+metadata+'</article>'
    else:
        tag={'adjacent':'','quote':'blockquote','navigation':'nav','other_article':'article'}[location]
        html='<article>'+content+'</article>'+('<'+tag+'>'+metadata+'</'+tag+'>' if tag else metadata)
    await page.set_content(html)
    controller=NativeAction.__new__(NativeAction)
    controller.action={'type':'reply','intent':{'body':'Original answer','parent_url':ORIGIN+'/records/42','browser_method':{'target_url':ORIGIN+'/board'}}}
    controller.actor=SimpleNamespace()
    controller.snapshot=lambda:({}, {'external_account_id':'member-A'}, {})
    async def document(current):
        return current.url
    controller.document=document
    result=await controller.read_receipt(page,{'author':'#author','body':'#body','url':'#url','parent':'#parent'})
    assert bool(result) is allowed


@pytest.mark.parametrize('kind',['anchor','submit','input','foreign','download','js_handler','js_url'])
async def test_dispatched_original_action_can_only_follow_actual_plain_receipt_link(common_page,monkeypatch,kind):
    import asyncio
    from contextlib import contextmanager

    from lead_app import native_action

    page,context,_=common_page
    await page.goto(ORIGIN+'/records/55?source=recent')
    await page.set_content('''<a id=anchor href="/records/55#entry">Canonical receipt</a>
      <form method=post action=/respond><input id=input name=body><button id=submit>Submit</button></form>
      <a id=foreign href="https://other.example.test/post">Outside</a><a id=download download href=/file>Download</a>
      <a id=js_handler onclick="fetch('/respond',{method:'POST'})" href=/records/55>Script control</a>
      <a id=js_url href="javascript:void(0)">Script</a>''')
    cdp=await context.new_cdp_session(page)
    try:
        target=(await cdp.send('Target.getTargetInfo'))['targetInfo']['targetId']
        document=(await cdp.send('DOM.getDocument'))['root']['nodeId']
        selected=(await cdp.send('DOM.querySelector',{'nodeId':document,'selector':'#'+kind}))['nodeId']
        backend=(await cdp.send('DOM.describeNode',{'nodeId':selected}))['node']['backendNodeId']
        gate=SimpleNamespace(targets={'session':{'targetId':target,'type':'page','url':page.url}},owners={'session':target})
        async def send(name,params,session):
            assert session=='session'
            return await cdp.send(name,params)
        gate.send=send
        async def scope_view(root):
            assert root==target
            return {'targets':{target:target},'origins':[ORIGIN]}
        async def validate_url(url):
            return None
        actor=SimpleNamespace(aid='00000000-0000-0000-0000-000000000066',lock=asyncio.Lock(),context=context,
            native_runtime=SimpleNamespace(scope_view=scope_view,gate=gate,bridge=SimpleNamespace(url='',headers={})),
            forward_proxy=SimpleNamespace(validate_url=validate_url))
        class Database:
            def execute(self,sql,values):
                return SimpleNamespace(fetchone=lambda:{'state':'dispatching'})
        @contextmanager
        def transaction(*args):
            yield Database()
        monkeypatch.setattr(native_action,'tx',transaction)
        controller=NativeAction.__new__(NativeAction)
        controller.actor=actor
        controller.task_id='original-task'
        controller.action_id='original-action'
        controller.lease={'target_id':target,'origins':[ORIGIN]}
        controller.reconcile=False
        controller.steps=controller.corrections=controller.total_corrections=0
        controller.phase=lambda name:None
        controller.action={'intent':{'browser_method':{'target_url':ORIGIN+'/board'}}}
        controller.page=page
        name='input' if kind=='input' else 'click'
        result=await controller.authorize({'name':name,'node':{'target_id':target,'backend_node_id':backend},'values':{}})
        assert bool(result.get('allowed')) is (kind=='anchor')
        if kind=='anchor':
            assert result['url']==ORIGIN+'/records/55#entry'
            await page.locator('#anchor').click()
            assert page.url==result['url']
        assert controller.reconcile is False
    finally:
        await cdp.detach()
