"""Complete current identity comes from DOM, never a saved credential or affix guess."""
import os

import pytest
from lead_app.native_identity_facts import inspect_current_identity
from test_native_declarative_forms_dom import ORIGIN, editor, inspect_editor
from test_native_declarative_forms_dom import common_page as shared_page

common_page=shared_page
pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',
    reason='Explicit isolated identity DOM verification required')]


@pytest.mark.parametrize('kind',['nested','adjacent','nav','unicode'])
async def test_server_extracts_full_subject_and_actual_affixes_from_unique_local_current_dom(common_page,kind):
    page,_,_=common_page
    subject='成员-甲' if kind=='unicode' else 'member-alpha'
    link='<a rel=me href=/private-self>Account settings</a>'
    html='<p>Signed in as <span>'+subject+'</span> · '+link+'</p>'
    if kind=='adjacent':
        html='<p>Signed in as '+subject+'</p>'+link
    elif kind=='nav':
        html='<header><nav>Signed in as '+subject+' · '+link+'</nav></header>'
    await page.set_content(html)
    facts=await inspect_current_identity(page,connection_origin=ORIGIN)
    assert facts['acting_subject']==subject
    assert facts['self_url']==ORIGIN+'/private-self'
    assert await page.locator(facts['acting_subject_selector']).inner_text()==facts['self_subject_prefix']+subject+facts['self_subject_suffix']
    assert await page.locator(facts['self_link_selector']).count()==1


@pytest.mark.parametrize('kind',['distant','two_subjects','two_links','article','quote','editable','foreign','two_values','unknown_tail','ugc_ancestor','editable_ancestor','hidden_ancestor','hidden_link'])
async def test_server_inference_refuses_ambiguous_public_editable_or_unrelated_identity(common_page,kind):
    page,_,_=common_page
    html='<p>Signed in as member-alpha · <a rel=me href=/private-self>Settings</a></p>'
    if kind=='distant':
        html='<main><p>Signed in as member-alpha</p><section><a rel=me href=/private-self>Settings</a></section></main>'
    elif kind=='two_subjects':
        html+='<p>Posting as other-member</p>'
    elif kind=='two_links':
        html+='<a rel=me href=/other-self>Other</a>'
    elif kind in ('article','quote','editable'):
        tag='article' if kind=='article' else 'blockquote' if kind=='quote' else 'div contenteditable=true'
        html='<'+tag+'>'+html+'</'+tag.split()[0]+'>'
    elif kind=='foreign':
        html=html.replace('href=/private-self','href=https://outside.example.test/private-self')
    elif kind=='hidden_link':
        html=html.replace('rel=me','rel=me style="opacity:0"')
    elif kind=='two_values':
        html=html.replace('member-alpha','member-alpha and other-member')
    elif kind in ('ugc_ancestor','editable_ancestor','hidden_ancestor'):
        start='<article>' if kind=='ugc_ancestor' else '<div contenteditable=true>' if kind=='editable_ancestor' else '<p style="opacity:0">'
        end='</article>' if kind=='ugc_ancestor' else '</div>' if kind=='editable_ancestor' else '</p>'
        html='<main>'+start+'Signed in as member-alpha'+end+'<a rel=me href=/private-self>Settings</a></main>'
    else:
        html=html.replace('member-alpha','member-alpha (unproved metadata)')
    await page.set_content(html)
    with pytest.raises(ValueError):
        await inspect_current_identity(page,connection_origin=ORIGIN)


async def test_broad_manual_proposal_cannot_borrow_a_distant_self_link(common_page):
    from lead_app.native_identity_facts import inspect_self_entry
    page,_,_=common_page
    await page.set_content('<main><p>Signed in as member-alpha</p><section><a rel=me href=/private-self>Settings</a></section></main>')
    text=await page.locator('main').inner_text()
    with pytest.raises(ValueError,match='identity_current_self_unestablished'):
        await inspect_self_entry(page,link_selector='a',subject_selector='main',subject_prefix='Signed in as ',
            subject_suffix=text[len('Signed in as member-alpha'):],connection_origin=ORIGIN)


@pytest.mark.parametrize('kind',['long_subject_id','long_link_id','long_css_path','long_prefix','long_suffix','empty_href','missing_href','optional_empty_href','no_link','normal'])
async def test_inferred_contract_obeys_original_selector_affix_and_href_bounds(common_page,kind):
    page,_,_=common_page
    html='<p id=current>Signed in as member-alpha · <a id=self rel=me href=/private-self>Settings</a></p>'
    await page.set_content(html)
    if kind in ('long_subject_id','long_link_id'):
        await page.locator('#current' if kind=='long_subject_id' else '#self').evaluate("node=>node.id='x'.repeat(500)")
    elif kind=='long_css_path':
        await page.locator('#current').evaluate("node=>{node.removeAttribute('id');for(let i=0;i<12;i++){const parent=document.createElement('long-custom-element-tag-name');node.before(parent);parent.append(node);node=parent;}}")
    elif kind=='long_suffix':
        await page.locator('#self').evaluate("node=>node.innerText='Settings'.repeat(30)")
    elif kind=='long_prefix':
        await page.locator('#current').evaluate("node=>{node.style.whiteSpace='pre';node.firstChild.textContent='Signed in as'+' '.repeat(200)+'member-alpha · '}")
    elif kind in ('empty_href','optional_empty_href'):
        await page.locator('#self').evaluate("node=>node.setAttribute('href','')")
    elif kind=='missing_href':
        await page.locator('#self').evaluate("node=>node.removeAttribute('href')")
    elif kind=='no_link':
        await page.locator('#self').evaluate('node=>node.remove()')
    kwargs={'connection_origin':ORIGIN,'require_self_link':kind not in ('optional_empty_href','no_link')}
    if kind in ('long_css_path','long_prefix','long_suffix','empty_href','missing_href','optional_empty_href'):
        with pytest.raises(ValueError):
            await inspect_current_identity(page,**kwargs)
    else:
        facts=await inspect_current_identity(page,**kwargs)
        assert facts['acting_subject']=='member-alpha'
        assert len(facts['acting_subject_selector'])<=300
        assert await page.locator(facts['acting_subject_selector']).count()==1
        if kind=='no_link':
            assert facts['self_link_selector'] is None
        else:
            assert len(facts['self_link_selector'])<=300
            assert await page.locator(facts['self_link_selector']).count()==1
        assert len(facts['self_subject_prefix'])<=150 and len(facts['self_subject_suffix'])<=150


async def test_current_identity_facts_do_not_survive_same_url_document_replacement(common_page,monkeypatch):
    page,_,_=common_page
    html='<p>Signed in as member-alpha · <a rel=me href=/private-self>Settings</a></p>'
    await page.set_content(html)
    original=page.evaluate_handle
    async def capture(*args,**kwargs):
        handle=await original(*args,**kwargs)
        class Changed:
            async def evaluate(self,script,*values):
                result=await handle.evaluate(script,*values)
                if script=='proof=>proof.facts':
                    await page.set_content(html)
                return result
            async def dispose(self):
                await handle.dispose()
        return Changed()
    monkeypatch.setattr(page,'evaluate_handle',capture)
    with pytest.raises(ValueError,match='identity_document_changed'):
        await inspect_current_identity(page,connection_origin=ORIGIN)


@pytest.mark.parametrize('wrong_subject',[False,True])
async def test_method_uses_full_server_subject_equal_to_verified_private_identity(common_page,wrong_subject):
    page,context,_=common_page
    proposal,kwargs,_=await editor(page,context)
    await page.locator('#current').evaluate("node=>node.innerHTML='Signed in as <span>member-A</span> · <a rel=me href=/account>Settings</a>'")
    proposal['self_subject_suffix']=' (model guessed suffix)'
    if wrong_subject:
        kwargs['external_account_id']='member'
        with pytest.raises(ValueError,match='identity_changed'):
            await inspect_editor(page,proposal,**kwargs)
    else:
        method=await inspect_editor(page,proposal,**kwargs)
        assert method['self_subject_suffix']==' · Settings'
        assert (await page.locator(method['selectors']['self_subject']).inner_text())==method['self_subject_prefix']+'member-A'+method['self_subject_suffix']


@pytest.mark.parametrize('stage',['method','prepare'])
@pytest.mark.parametrize('change',['hidden_old_visible_other','two_visible_current','late_second_subject','normal','manual_unknown_metadata'])
async def test_manual_identity_match_never_bypasses_current_complete_dom_facts(common_page,stage,change,monkeypatch):
    from types import SimpleNamespace

    from lead_app import native_worker_controller
    from lead_app.native_action import NativeAction

    page,context,_=common_page
    proposal,kwargs,parent=await editor(page,context)
    method=await inspect_editor(page,proposal,**kwargs)
    if change=='hidden_old_visible_other':
        await page.locator('#current').evaluate("node=>node.style.opacity='0'")
    if change in ('hidden_old_visible_other','two_visible_current'):
        await page.locator('main').evaluate("node=>node.insertAdjacentHTML('beforeend','<p>Posting as member-B</p>')")
    elif change=='manual_unknown_metadata':
        await page.locator('#current').evaluate("node=>node.innerText='Signed in as member-A (Display name)'")
        proposal['self_subject_suffix']=' (Display name)'
        method['self_subject_suffix']=' (Display name)'
    if change=='late_second_subject' and stage=='method':
        from lead_app import native_method_facts
        original=native_method_facts._form_facts
        async def mutate(*args):
            facts=await original(*args)
            if await page.locator('#late-subject').count()==0:
                await page.locator('main').evaluate("node=>node.insertAdjacentHTML('beforeend','<p id=late-subject>Posting as member-B</p>')")
            return facts
        monkeypatch.setattr(native_method_facts,'_form_facts',mutate)
    async def run():
        if stage=='method':
            return await inspect_editor(page,proposal,**kwargs)
        controller=NativeAction.__new__(NativeAction)
        controller.page=page
        controller.lease={'target_id':'original-target','document_revision':1}
        controller.runtime=SimpleNamespace()
        controller.snapshot=lambda:({}, {'external_account_id':'member-A'}, {})
        controller.action={'type':'reply','intent':{'browser_method':method,'body':'Original answer','parent_url':parent}}
        controller.submit_proof=None
        await page.locator('textarea').fill('Original answer')
        async def descriptor():
            return {'target_id':'original-target'}
        documents=0
        async def document():
            nonlocal documents
            documents+=1
            if change=='late_second_subject' and documents==2:
                await page.locator('main').evaluate("node=>node.insertAdjacentHTML('beforeend','<p>Posting as member-B</p>')")
            cdp=await context.new_cdp_session(page)
            try:
                frame=(await cdp.send('Page.getFrameTree'))['frameTree']['frame']
                return ('original-root',[(frame['id'],frame['loaderId'],frame['url'])])
            finally:
                await cdp.detach()
        async def no_worker(*args):
            return None
        controller.descriptor=descriptor
        controller.document=document
        monkeypatch.setattr(native_worker_controller,'refresh_page_controller',no_worker)
        await controller.prepare_submit()
        return controller.submit_proof
    if change=='normal':
        assert await run()
    else:
        with pytest.raises(ValueError):
            await run()


@pytest.mark.parametrize('hidden',['visible','field','ancestor'])
async def test_private_identity_value_must_be_visible_including_ancestor_opacity(common_page,hidden):
    from lead_app.native_identity_facts import inspect_private_subject
    page,_,_=common_page
    await page.set_content('<p>Account ID: <strong id=subject>member-A</strong></p>')
    if hidden!='visible':
        await page.locator('#subject').evaluate("(node,ancestor)=>(ancestor?node.parentElement:node).style.opacity='0'",hidden=='ancestor')
        with pytest.raises(ValueError):
            await inspect_private_subject(page,subject_selector='#subject')
    else:
        assert (await inspect_private_subject(page,subject_selector='#subject'))['subject']=='member-A'
