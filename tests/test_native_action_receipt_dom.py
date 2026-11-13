"""Receipt field relationships in a real DOM, including misleading global links."""
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from lead_app.native_action import NativeAction
from playwright.async_api import async_playwright

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',
    reason='Explicit installed browser test runtime required')]


@pytest_asyncio.fixture
async def receipt_page():
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True,executable_path=playwright.chromium.executable_path)
        page=await browser.new_page()
        await page.route('**/*',lambda route:route.fulfill(body='<main></main>',content_type='text/html'))
        await page.goto('https://board.example.test/posts/7')
        controller=NativeAction.__new__(NativeAction)
        controller.actor=SimpleNamespace()
        controller.action={'type':'reply','intent':{'body':'A controlled reply','parent_url':'https://board.example.test/posts/1',
            'browser_method':{'target_url':'https://board.example.test/home'}}}
        controller.snapshot=lambda:({}, {'external_account_id':'member-A'}, {})
        async def document(current):
            return current.url
        controller.document=document
        html='''<main><article><p>Author: <span id="author">member-A</span></p>
            <div id="body">A controlled reply</div><a id="url" href="/posts/7">Reply URL</a></article>
            <a id="parent" href="/posts/1">Parent post</a></main>'''
        await page.set_content(html)
        try:
            yield controller,page,html,{'author':'#author','body':'#body','url':'#url','parent':'#parent'}
        finally:
            await browser.close()


async def test_adjacent_explicit_parent_belongs_to_the_receipt(receipt_page):
    controller,page,_,selectors=receipt_page
    assert (await controller.read_receipt(page,selectors))['parent_url']==controller.action['intent']['parent_url']
    await page.locator('article').evaluate("node=>node.append(document.querySelector('#parent'))")
    assert await controller.read_receipt(page,selectors)


async def test_global_correct_parent_cannot_confirm_a_wrong_parent_reply(receipt_page):
    controller,page,_,selectors=receipt_page
    await page.locator('#parent').evaluate("node=>{node.id='actual-parent';node.href='/posts/2'; const nav=document.createElement('nav');nav.innerHTML='<a id=parent href=/posts/1>Parent post</a>';document.querySelector('main').append(nav)}")
    assert await controller.read_receipt(page,selectors) is None
    selectors['parent']='#actual-parent'
    assert await controller.read_receipt(page,selectors) is None


async def test_unrelated_article_parent_and_quote_do_not_confirm(receipt_page):
    controller,page,_,selectors=receipt_page
    await page.locator('#parent').evaluate("node=>{const other=document.createElement('article');node.replaceWith(other);other.append(node)}")
    assert await controller.read_receipt(page,selectors) is None
