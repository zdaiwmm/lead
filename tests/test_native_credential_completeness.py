"""Actual form validity facts, without dispatching credentials or invalid events."""
from types import SimpleNamespace

import pytest
from lead_app.native_connection import FORM_FACTS, NativeConnection
from playwright.async_api import async_playwright


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True,channel='chromium')
        current=await browser.new_page()
        await current.route('**/*',lambda route:route.fulfill(body='<main></main>',content_type='text/html'))
        await current.goto('http://credential.native-dom.test/login')
        try:
            yield current
        finally:
            await browser.close()


@pytest.mark.parametrize('fields,allowed,reason,form_attrs,button_attrs',[
    ('<input required value="member"><input type=password required>',False,'empty password','',''),
    ('<input required><input type=password required value="synthetic">',False,'empty required','',''),
    ('<input type=email required value="invalid"><input type=password value="synthetic">',False,'form is invalid','',''),
    ('<input><input type=password value="synthetic">',True,None,'',''),
    ('<input required value="member"><input type=password required value="synthetic">',True,None,'',''),
    ('<input><input type=password>',False,'empty password','',''),
    ('<input type=email required value="member"><input type=password value="synthetic">',True,None,'novalidate',''),
    ('<input type=email required value="member"><input type=password value="synthetic">',True,None,'','formnovalidate'),
    ('<input type=email required value="member"><input type=password value="synthetic">',True,None,'','type=button'),
    ('<input required><input type=password value="synthetic">',True,None,'novalidate',''),
    ('<input required value="member"><input type=password>',False,'empty password','novalidate',''),
    ('<input required value="member"><input type=password>',False,'empty password','','type=button'),
])
@pytest.mark.asyncio
async def test_only_actual_missing_or_invalid_credential_form_prevents_submit(page,fields,allowed,reason,form_attrs,button_attrs):
    await page.set_content('<h1>Sign in</h1><form method=post action=/session '+form_attrs+'>'+fields+'<button '+button_attrs+'>Sign in</button></form>')
    await page.evaluate('''()=>{window.invalidEvents=0;document.addEventListener('invalid',()=>window.invalidEvents++,true)}''')
    facts=await page.locator('button').evaluate('node=>('+FORM_FACTS+').call(node)')
    assert not any('value' in key.lower() for key in facts)
    controller=NativeConnection(SimpleNamespace(native_runtime=None),'task','connection')
    controller.page=page
    controller.lease={'origins':['http://credential.native-dom.test'],'authentication_endpoints':[facts['action']]}
    async def descriptor():
        return {}
    async def node_facts(node):
        return facts
    async def auth_control(node):
        return None
    controller.descriptor=descriptor
    controller.node_facts=node_facts
    controller.auth_control=auth_control
    result=await controller.authorize({'name':'click','node':{},'values':{}})
    assert result['allowed'] is allowed
    if reason:
        assert reason in result['reason']
    assert await page.evaluate('window.invalidEvents')==0
