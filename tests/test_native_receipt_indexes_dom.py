"""Finite receipt index facts stay on one original attempt; no model or send.

DOM/screenshot reads are real. The original attempt/action persistence and live
page authority are explicit seams; publication, grants and claim SQL are not run.
"""
import copy
import os
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from lead_app.native_action import NativeAction
from test_native_declarative_forms_dom import ORIGIN
from test_native_declarative_forms_dom import common_page as shared_page

common_page=shared_page

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',
    reason='Explicit isolated receipt DOM verification required')]


async def receipt_controller(common_page,monkeypatch):
    from lead_app import native_action

    page,context,requests=common_page
    root=ORIGIN+'/editor'
    index=ORIGIN+'/catalog'
    state={'new':False,'changed_index':False,'action_state':'prepared','wide':False}
    visited=[]
    async def route(current):
        path=current.request.url
        if path==root:
            links='<nav><a href="'+('/catalog-two' if state['changed_index'] else '/catalog')+'">Read entries</a>'
            links+='<a href=/login>Login</a><a href=/delete>Delete</a><a href=/follow>Follow</a><a href=/invite>Invite</a>'
            links+='<a rel=me href=/account>Account</a><a href=https://other.example.test/archive>Outside</a></nav>'
            body=links+(''.join('<a href=/old/'+str(i)+'>Old</a>' for i in range(300)) if state['wide'] else '')
        elif path in (index,ORIGIN+'/catalog-two'):
            body='<main><a href=/records/44>Older post</a>'
            if state['new']:
                body+='<a href=/records/55>New answer</a>'
            if state['wide']:
                body+=''.join('<a href=/archived/'+str(i)+'>Old</a>' for i in range(300))
            body+='</main>'
        elif path==ORIGIN+'/records/55':
            body='<article><p>Author: <span id=author>member-A</span></p><div id=body>New answer</div><a id=url rel=canonical href=/records/55>Canonical</a></article>'
        else:
            body='<p>Unsupported source</p>'
        await current.fulfill(body=body,content_type='text/html')
    await context.unroute('**/*')
    await context.route('**/*',route)
    attempt={'id':str(uuid4()),'attempt':1,'observation':{'browser_request':{'claimed':False}},'outcome':None}
    class Result:
        def __init__(self,value):
            self.value=value
        def fetchone(self):
            return copy.deepcopy(self.value)
    class Database:
        def execute(self,sql,values):
            if sql.startswith('SELECT'):
                return Result({'state':state['action_state']} if 'FROM lead.actions ' in sql else attempt)
            if sql.startswith('UPDATE lead.action_attempts SET observation='):
                attempt['observation'].update(copy.deepcopy(values[0]))
            elif sql.startswith('UPDATE lead.action_attempts SET outcome='):
                attempt['observation'].update(copy.deepcopy(values[0]))
                attempt['outcome']='confirmed'
            elif sql.startswith('UPDATE lead.actions SET state='):
                state['action_state']='confirmed'
            return Result(None)
    @contextmanager
    def transaction(*args):
        yield Database()
    monkeypatch.setattr(native_action,'tx',transaction)
    monkeypatch.setattr(native_action,'j',lambda value:value)
    controller=NativeAction.__new__(NativeAction)
    controller.actor=SimpleNamespace(aid='00000000-0000-0000-0000-000000000082')
    controller.action_id=str(uuid4())
    controller.action={'type':'publish','intent':{'body':'New answer','browser_method':{'target_url':root,'self_url':ORIGIN+'/account'}}}
    controller.lease={'target_id':'original-root','read_urls':[root]}
    controller.reconcile=False
    controller.receipt=None
    controller.snapshot=lambda:({}, {'external_account_id':'member-A'}, {})
    pages={}
    async def temporary(url):
        visited.append(url)
        current=await context.new_page()
        await current.goto(url)
        target=str(uuid4())
        pages[target]=current
        return current,{'target_id':target}
    async def release(target):
        await pages.pop(target).close()
    controller.temporary_page=temporary
    controller.actor.native_runtime=SimpleNamespace(release_page=release,current_scope=lambda scope:None)
    async def document(current=None,scope=None):
        current=current or controller.page
        cdp=await context.new_cdp_session(current)
        try:
            frame=(await cdp.send('Page.getFrameTree'))['frameTree']['frame']
            return frame['id'],frame['loaderId'],frame['url']
        finally:
            await cdp.detach()
    controller.document=document
    async def descriptor():
        return {'target_id':'original-root','origins':[ORIGIN]}
    controller.descriptor=descriptor
    return controller,attempt,state,visited,page,root,index


async def test_attempt_freezes_first_level_safe_index_and_confirms_unique_new_receipt(common_page,monkeypatch):
    controller,attempt,state,visited,page,root,index=await receipt_controller(common_page,monkeypatch)
    await controller.baseline()
    frozen=attempt['observation']['browser_baseline']
    assert frozen['index_pages']==[root,index]
    assert ORIGIN+'/records/44' in frozen['urls'] and ORIGIN+'/records/55' not in frozen['urls']
    assert all(url not in visited for url in (ORIGIN+'/login',ORIGIN+'/delete',ORIGIN+'/follow',ORIGIN+'/invite',ORIGIN+'/account'))
    state.update(new=True,action_state='unknown')
    await page.goto(ORIGIN+'/records/55')
    controller.page=page
    controller.reconcile=True
    result=await controller.report_receipt({'selectors':{'author':'#author','body':'#body','url':'#url'}})
    assert result['state']=='confirmed' and attempt['attempt']==1 and attempt['outcome']=='confirmed'
    assert attempt['observation']['browser_baseline']==frozen


async def test_existing_baseline_does_not_expand_to_newly_linked_indexes(common_page,monkeypatch):
    controller,attempt,state,visited,page,root,index=await receipt_controller(common_page,monkeypatch)
    await controller.baseline()
    state['changed_index']=True
    visited.clear()
    await controller.baseline()
    assert attempt['observation']['browser_baseline']['index_pages']==[root,index]
    assert ORIGIN+'/catalog-two' not in visited


async def test_legacy_single_page_baseline_cannot_adopt_a_later_index(common_page,monkeypatch):
    controller,attempt,state,visited,page,root,index=await receipt_controller(common_page,monkeypatch)
    attempt['observation']['browser_baseline']={'url':root,'urls':[root,index],'bounded':True}
    state.update(new=True,action_state='unknown')
    await page.goto(ORIGIN+'/records/55')
    controller.page=page
    controller.reconcile=True
    with pytest.raises(ValueError,match='browser_receipt_not_unique'):
        await controller.report_receipt({'selectors':{'author':'#author','body':'#body','url':'#url'}})
    assert index not in visited and state['action_state']=='unknown'
    assert 'index_pages' not in attempt['observation']['browser_baseline']


async def test_collection_limit_applies_to_the_whole_frozen_set(common_page,monkeypatch):
    controller,attempt,state,visited,page,root,index=await receipt_controller(common_page,monkeypatch)
    state['wide']=True
    with pytest.raises(ValueError,match='baseline_ambiguous'):
        await controller.baseline()
    assert 'browser_baseline' not in attempt['observation']


async def test_legacy_redirected_baseline_keeps_its_original_single_url(common_page,monkeypatch):
    controller,attempt,state,visited,page,root,index=await receipt_controller(common_page,monkeypatch)
    legacy=ORIGIN+'/older-canonical-editor'
    attempt['observation']['browser_baseline']={'url':legacy,'urls':[legacy],'bounded':True}
    await controller.baseline()
    assert visited==[legacy]
    assert attempt['observation']['browser_baseline']=={'url':legacy,'urls':[legacy],'bounded':True}
