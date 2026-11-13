"""Actual B rule DOM and the exact proposal selected by the real Codex turn."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from lead_app.native_method_facts import inspect_editor
from native_browser_fixture_b import BlindBoard
from playwright.async_api import async_playwright

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',
    reason='Explicit isolated B DOM verification required')]


@pytest_asyncio.fixture
async def observed_b_rules():
    board=BlindBoard().start()
    async with async_playwright() as tools:
        options={'headless':True,'args':['--no-sandbox','--disable-background-networking']}
        asset=Path(os.environ.get('LEAD_BROWSER_RUNTIME_DIR','/missing'))/'chromium/chrome-linux64/chrome'
        if asset.is_file():
            options['executable_path']=str(asset)
        browser=await tools.chromium.launch(**options)
        context=await browser.new_context(service_workers='block')
        async def local_only(route):
            await route.continue_() if route.request.url.startswith(board.origin+'/') else await route.abort()
        await context.route('**/*',local_only)
        page=await context.new_page()
        await page.goto(board.origin+'/access')
        await page.locator('input[name=member_name]').fill(board.state['username'])
        await page.locator('input[type=password]').fill(board.state['password'])
        await page.locator('button[type=submit]').click()
        await page.wait_for_url(board.origin+'/desk')
        permission=await context.new_page()
        await permission.goto(board.origin+'/house-rules')
        rule=permission.locator('section:has-text("For regular members")')
        paragraphs=await rule.locator('p').all_inner_texts()
        proposal={'target_url':board.origin+'/house-rules','operation':'publish','selectors':{
            'submit':'button[type="submit"]','editor_body':'textarea#community-words[name="entry_words"]',
            'editor_target':f'a[href="{board.origin}/house-rules"]:has-text("Read current community rules")'},
            'permission_quote':' '.join(paragraphs[:2]),'permission_selector':'section:has-text("For regular members")'}
        kwargs={'account_id':str(uuid4()),'binding_version':1,'external_account_id':board.state['username'],
            'connection_origin':board.origin,'operation':'publish','evidence_ref':'actual-b-dom-rules',
            'permission_page':permission}
        try:
            yield page,permission,proposal,kwargs,board
        finally:
            await browser.close()
            board.close()


async def test_actual_b_house_rules_and_model_selected_selectors_prove_permission(observed_b_rules):
    page,_,proposal,kwargs,board=observed_b_rules
    method=await inspect_editor(page,{k:v for k,v in proposal.items() if k!='operation'},**kwargs)
    assert method['platform_permission']['state']=='approved'
    assert method['platform_permission']['source_url']==board.origin+'/house-rules'
    assert method['request']['content_field']=='entry_words'
    assert sum(row['method']=='POST' for row in board.oracle()['requests'])==1


@pytest.mark.parametrize('change',['denied','assistant_denied','conditional','approval','quoted','user_post','wrong_current_subject'])
async def test_actual_rule_wording_cannot_bypass_other_current_dom_gates(observed_b_rules,change):
    page,permission,proposal,kwargs,board=observed_b_rules
    if change=='wrong_current_subject':
        await page.locator('nav span').evaluate("node=>node.innerText='Signed in as another.member'")
    elif change in ('quoted','user_post'):
        tag='blockquote' if change=='quoted' else 'article'
        await permission.locator('section').evaluate('(node,tag)=>{const outer=document.createElement(tag);node.replaceWith(outer);outer.append(node)}',tag)
    else:
        tail={'denied':'Members must not publish or reply using browser automation.',
            'assistant_denied':'Browser assistants are not allowed.',
            'conditional':'Members may use browser automation to publish and reply only if administrator approval is given.',
            'approval':'Prior owner approval is required before doing so.'}[change]
        await permission.locator('section').evaluate('(node,text)=>{const p=document.createElement("p");p.textContent=text;node.append(p)}',tail)
    with pytest.raises(ValueError):
        await inspect_editor(page,{k:v for k,v in proposal.items() if k!='operation'},**kwargs)
    assert sum(row['method']=='POST' for row in board.oracle()['requests'])==1
