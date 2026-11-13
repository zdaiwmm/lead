"""Real reply DOM: Playwright selector semantics must authorize only its exact node."""
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from lead_app import native_action
from lead_app.native_action import NativeAction
from test_native_member_automation_dom import observed_b_rules  # noqa: F401

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',
    reason='Explicit isolated selector identity DOM verification required')]


@pytest_asyncio.fixture
async def reply_guard(observed_b_rules,monkeypatch):  # noqa: F811
    page,_,_,_,board=observed_b_rules
    await page.locator('#community-words').fill('An actual parent for independent selector identity verification.')
    await page.locator('button[type=submit]').click()
    await page.wait_for_url(board.origin+'/bulletin/7001')
    cdp=await page.context.new_cdp_session(page)
    target=(await cdp.send('Target.getTargetInfo'))['targetInfo']['targetId']
    async def send(method,args,sid):
        assert sid=='current'
        return await cdp.send(method,args)
    gate=SimpleNamespace(send=send,targets={'current':{'targetId':target}},owners={'current':target})
    value=NativeAction.__new__(NativeAction)
    value.action_id='guard-only'
    value.actor=SimpleNamespace(aid='guard-only',native_runtime=SimpleNamespace(gate=gate))
    value.page=page
    value.lease={'target_id':target}
    value.action={'intent':{'body':'An exact authorized reply body.','browser_method':{'selectors':{
        'editor_body':'textarea#reply-words[name="response_words"]',
        'submit':'button[type="submit"]:text-is("Publish reply")'}}}}
    value.reconcile=False
    value.local_executor=True
    value.steps=0
    value.phase=lambda _name:None
    value.descriptor=AsyncMock(return_value={'targets':{target:target}})
    value.prepare_submit=AsyncMock()
    async def document():
        return await page.evaluate('()=>[location.href,performance.timeOrigin]')
    value.document=document
    @contextmanager
    def transaction(_aid):
        yield None
    monkeypatch.setattr(native_action,'tx',transaction)
    monkeypatch.setattr(native_action,'one',lambda *_args:{'state':'prepared'})
    async def node(expression):
        remote=await cdp.send('Runtime.evaluate',{'expression':expression,'returnByValue':False})
        identifier=remote['result']['objectId']
        try:
            result=await cdp.send('DOM.describeNode',{'objectId':identifier})
            return {'target_id':target,'backend_node_id':result['node']['backendNodeId']}
        finally:
            await cdp.send('Runtime.releaseObject',{'objectId':identifier})
    try:
        yield value,page,board,node
    finally:
        await cdp.detach()


@pytest.mark.parametrize('name,expression',[
    ('input',"document.querySelector('#reply-words')"),
    ('click',"document.querySelector('button[type=submit]')")])
async def test_actual_frozen_reply_selector_extension_accepts_exact_control_only(reply_guard,name,expression):
    value,_,board,node=reply_guard
    args={'name':name,'node':await node(expression),'values':{'text':value.action['intent']['body']}}
    result=await value.authorize(args)
    assert result['allowed'] is True
    assert value.prepare_submit.call_count==(1 if name=='click' else 0)
    assert sum(row['method']=='POST' for row in board.oracle()['requests'])==2


async def test_extended_body_selector_uses_mature_playwright_semantics(reply_guard):
    value,_,_,node=reply_guard
    value.action['intent']['browser_method']['selectors']['editor_body']='textarea[name="response_words"]:visible'
    result=await value.authorize({'name':'input','node':await node("document.querySelector('#reply-words')"),'values':{'text':value.action['intent']['body']}})
    assert result['allowed'] is True


@pytest.mark.parametrize('change',['other_button','duplicate','wrong_body','foreign_target','detached'])
async def test_no_selector_stripping_attribute_or_text_fallback_can_authorize_another_node(reply_guard,change):
    value,page,_,node=reply_guard
    operation='click' if change=='other_button' else 'input'
    if change=='other_button':
        await page.locator('form').evaluate("form=>form.append(Object.assign(document.createElement('button'),{type:'submit',id:'different',textContent:'Other operation'}))")
        actual=await node("document.querySelector('#different')")
    else:
        actual=await node("document.querySelector('#reply-words')")
    if change=='duplicate':
        await page.locator('#reply-words').evaluate("node=>node.after(node.cloneNode(true))")
    elif change=='foreign_target':
        actual['target_id']='another-owned-or-foreign-page'
    elif change=='detached':
        await page.locator('#reply-words').evaluate('node=>node.remove()')
    result=await value.authorize({'name':operation,'node':actual,'values':{'text':'Other body' if change=='wrong_body' else value.action['intent']['body']}})
    assert result['allowed'] is False
    value.prepare_submit.assert_not_called()


async def test_open_shadow_control_keeps_full_playwright_selector_identity(reply_guard):
    value,page,_,node=reply_guard
    await page.locator('main').evaluate("main=>{const host=document.createElement('section');host.id='shadow-editor';main.append(host);host.attachShadow({mode:'open'}).innerHTML='<textarea name=original-body></textarea>'}")
    value.action['intent']['browser_method']['selectors']['editor_body']='#shadow-editor textarea[name=original-body]:visible'
    actual=await node("document.querySelector('#shadow-editor').shadowRoot.querySelector('textarea')")
    result=await value.authorize({'name':'input','node':actual,'values':{'text':value.action['intent']['body']}})
    assert result['allowed'] is True


@pytest.mark.parametrize('change',['replace','navigation','label_change'])
async def test_actual_element_replacement_document_or_selector_change_cannot_cross_identity_reads(reply_guard,change):
    from playwright.async_api import Error as PlaywrightError

    value,page,_,node=reply_guard
    operation='click' if change=='label_change' else 'input'
    expression="document.querySelector('button[type=submit]')" if operation=='click' else "document.querySelector('#reply-words')"
    actual=await node(expression)
    gate=value.actor.native_runtime.gate
    original=gate.send
    changed=False
    async def changing(method,args,sid):
        nonlocal changed
        result=await original(method,args,sid)
        if method=='Runtime.callFunctionOn' and 'const identity=' in args.get('functionDeclaration','') and not changed:
            changed=True
            if change=='replace':
                await page.locator('#reply-words').evaluate('node=>node.replaceWith(node.cloneNode(true))')
            elif change=='navigation':
                await page.goto(page.url)
            else:
                await page.locator('button[type=submit]').evaluate("node=>node.textContent='Another operation'")
        return result
    gate.send=changing
    try:
        result=await value.authorize({'name':operation,'node':actual,'values':{'text':value.action['intent']['body']}})
        assert result['allowed'] is False
    except PlaywrightError:
        assert change=='navigation'  # Destroyed remote context is also fail-closed.
    value.prepare_submit.assert_not_called()


async def test_standard_method_repair_proposal_identifies_the_same_original_submit(reply_guard):
    value,page,_,node=reply_guard
    selectors=value.action['intent']['browser_method']['selectors']
    previous=page.locator(selectors['submit'])
    assert await previous.count()==1
    pinned=await previous.element_handle()
    try:
        # A new independently validated form/body relationship, not stripping a
        # text pseudo-class from the original selector.
        proposal='form:has(> '+selectors['editor_body']+') > button[type="submit"]'
        proposed=page.locator(proposal)
        assert await proposed.count()==1
        assert await proposed.evaluate('(current,original)=>current===original',pinned)
        selectors['submit']=proposal
        result=await value.authorize({'name':'click','node':await node("document.querySelector('button[type=submit]')"),'values':{}})
        assert result['allowed'] is True
    finally:
        await pinned.dispose()
