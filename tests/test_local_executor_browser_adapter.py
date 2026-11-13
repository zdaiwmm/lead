"""Local Codex browser tools keep the original server-side execution identity."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lead_app import local_executor_browser as adapter


def test_tool_shapes_are_operation_specific_and_never_expose_browser_transport():
    connection = {tool['name']: tool for tool in adapter.browser_tools('connection')}
    discovery = {tool['name']: tool for tool in adapter.browser_tools('discovery')}
    action = {tool['name']: tool for tool in adapter.browser_tools('action')}
    assert 'report_connection' in connection and 'propose_method' not in connection
    assert 'propose_method' in discovery and 'browser_input' not in discovery
    assert 'report_receipt' in action and 'browser_input' in action
    for tools in (connection, discovery, action):
        assert 'browser_observe' in tools
        assert not any(key in str(tools) for key in ('cdp_url', 'gateway_token', 'saved_password_value'))
        assert all(tool['inputSchema'].get('additionalProperties') is False for tool in tools.values())


def test_request_binding_cannot_cross_device_agent_or_task():
    request = {'id':'r', 'agent_id':'a', 'tenant_id':'t', 'device_id':'d',
        'payload': {'tool_context': {'kind':'discovery','agent_id':'a','task_id':'k','run_id':'k',
            'profile_id':'p','task_generation':1,'attempt_token':'attempt','control_version':4}}}
    device = {'id':'d','agent_id':'a','tenant_id':'t'}
    assert adapter.request_context(device, request)['task_id'] == 'k'
    for key, value in [('id','other'),('agent_id','other'),('tenant_id','other')]:
        with pytest.raises(ValueError, match='executor_browser_scope_changed'):
            adapter.request_context({**device,key:value},request)
    request['payload']['tool_context']['agent_id']='other'
    with pytest.raises(ValueError, match='executor_browser_scope_changed'):
        adapter.request_context(device,request)


@pytest.mark.asyncio
async def test_pending_request_is_reused_and_native_page_stays_owned(monkeypatch):
    calls=[]
    controller=SimpleNamespace(actor=SimpleNamespace(aid='a',profile={'id':'p'}),task_id='task',
        lease={'task_id':'task','task_generation':1,'claim':{'attempt_token':'attempt'},'control_version':3},
        page=SimpleNamespace(url='https://fixture.test/board'),local_executor_request_id='r')
    async def status(aid, reference):
        calls.append((aid,reference))
        return {'id':reference,'state':'running'}
    monkeypatch.setattr(adapter,'_request_status',status)
    result=await adapter.run(controller,'https://gateway','discovery','Inspect actual editor')
    assert result == {'engine':'codex','executor_pending':True,'executor_request_id':'r'}
    assert calls == [('a','r')]
    assert controller.page is not None


@pytest.mark.asyncio
async def test_browser_request_uses_original_lease_control_not_observation_projection(monkeypatch):
    from contextlib import contextmanager

    from lead_app import db, local_executor

    recorded=[]
    @contextmanager
    def transaction(*args):
        yield SimpleNamespace(execute=lambda *args:None)
    monkeypatch.setattr(db,'tx',transaction)
    def submit(aid,key,payload,**kwargs):
        recorded.append(payload)
        return {'id':'original-request'}
    monkeypatch.setattr(local_executor,'submit',submit)
    task={'input':{'task_generation':2},'result':{'runtime':{'attempt_token':'attempt'}}}
    controller=SimpleNamespace(actor=SimpleNamespace(aid='a',profile={'id':'p'}),task_id='task',
        lease={'control_version':7},start=AsyncMock(return_value={'task_id':'task','generation':9,'target_id':'target'}),
        snapshot=lambda:(task,{},{}),page=SimpleNamespace(url='https://fixture.test/'))
    result=await adapter.run(controller,'https://gateway','action','Submit original action')
    assert result['executor_pending']
    assert recorded[0]['tool_context']['control_version']==7
    assert recorded[0]['tool_context']['task_generation']==2


@pytest.mark.parametrize('context,tools',[
    ({'kind':'model'},{'public_web':False,'community_research':True,'advanced_research':True}),
    ({'kind':'model','mode':'community_discovery'},{'public_web':True,'community_research':False,'advanced_research':True}),
    ({'kind':'model','browser_run_id':'run'},{'public_web':True,'community_research':True,'advanced_research':False}),
])
@pytest.mark.asyncio
async def test_disabling_research_after_queueing_prevents_actual_fetch(monkeypatch,context,tools):
    from lead_app import admin_settings, fetch, local_executor_tools

    monkeypatch.setattr(admin_settings,'for_agent',lambda *args:({'tools':tools},'current'))
    read=AsyncMock()
    monkeypatch.setattr(fetch,'fetch_product',read)
    device={'id':'d','agent_id':'a','tenant_id':'t'}
    request={'id':'r','device_id':'d','agent_id':'a','tenant_id':'t','payload':{'tool_context':context}}
    with pytest.raises(ValueError,match='executor_public_research_disabled'):
        await local_executor_tools.call(device,request,'lead_public_read',{'url':'https://example.com/'})
    read.assert_not_awaited()


def test_codex_controller_does_not_inherit_old_agent_fragment_limits():
    from lead_app.native_task import NativeTask
    actor=SimpleNamespace(native_runtime=None)
    controller=NativeTask(actor,'task')
    controller.local_executor=True
    controller.steps=1000
    controller.corrections=1000
    controller.total_corrections=1000
    controller.deadline=0
    assert controller.remaining_seconds() > 0
    assert not controller.progress_exhausted()
    controller.local_executor=False
    assert controller.progress_exhausted()
    with pytest.raises(ValueError,match='native_fragment_deadline'):
        controller.remaining_seconds()


@pytest.mark.asyncio
async def test_terminal_failure_is_not_reported_as_business_success(monkeypatch):
    async def status(*args):
        return {'state':'execution_unknown','result':{'output':'no proof'}}
    monkeypatch.setattr(adapter,'_request_status',status)
    controller=SimpleNamespace(actor=SimpleNamespace(aid='a'),local_executor_request_id='r')
    result=await adapter.run(controller,'https://gateway','action','Submit original action')
    assert result['success'] is False
    assert result['execution_state']=='execution_unknown'


@pytest.mark.parametrize('state',('prepared','dispatching','unknown','confirmed'))
@pytest.mark.asyncio
async def test_action_poll_keeps_original_pending_controller_after_submit(monkeypatch,state):
    from contextlib import contextmanager

    from lead_app import (
        channel_contracts,
        execution_routes,
        native_action,
        native_runner,
    )

    writes=[]
    @contextmanager
    def transaction(*args):
        yield SimpleNamespace(execute=lambda *args:writes.append(args))
    action={'state':state,'type':'publish','intent':{'body':'original body','browser_method':{
        'target_url':'https://fixture.test/board'},'execution_ref':{}}}
    claim={'attempt_token':'same-attempt'}
    controller=SimpleNamespace(action_id='original-action',local_executor_request_id='original-request',
        task_id='original-task',claim=claim,reconcile=False,receipt=None,operation_lock=__import__('asyncio').Lock(),
        close=AsyncMock())
    actor=SimpleNamespace(aid='a',task_pages={'original-task':controller})
    monkeypatch.setattr(native_action,'tx',transaction)
    monkeypatch.setattr(native_action,'one',lambda *args:action)
    monkeypatch.setattr(execution_routes,'endpoint',lambda *args:'https://gateway')
    monkeypatch.setattr(channel_contracts.ExecutionRef,'model_validate',lambda value:value)
    execute=AsyncMock(return_value={'engine':'codex','executor_pending':True,'executor_request_id':'original-request'})
    monkeypatch.setattr(native_runner,'run_task',execute)
    result=await native_action._execute_native_locked(actor,'original-action',claim)
    assert result['executor_pending']
    execute.assert_awaited_once()
    assert execute.await_args.args[0] is controller
    assert actor.task_pages['original-task'] is controller
    assert not writes
    controller.close.assert_not_awaited()


def test_actual_public_read_source_shape_and_producer_are_preserved(monkeypatch):
    from contextlib import contextmanager

    from lead_app import db, local_executor_tools

    writes=[]
    @contextmanager
    def transaction(*args):
        yield SimpleNamespace(execute=lambda *args:writes.append(args))
    monkeypatch.setattr(db,'tx',transaction)
    monkeypatch.setattr(db,'require_agent',lambda *args:{'control_version':4,'config_revision':2})
    monkeypatch.setattr(db,'one',lambda c,sql,args: {'agent_id':'a','id':'run','state':'running',
        'control_version':4,'config_revision':2} if 'browser_runs' in sql else {'n':1})
    observed={'url':'https://source.test/','title':'Actual source','text':'Original source text',
        'links':['https://t.me/observed_group','https://example.com/actual-link']}
    result=local_executor_tools._record_public_read({'agent_id':'a'},{'browser_run_id':'run'},observed)
    assert result['text']=='Original source text'
    assert result['engine']=='codex-web'
    assert result['community_links']==[{'href':'https://t.me/observed_group','name':''}]
    assert result['run_id']=='run' and result['evidence_id']
    assert 'codex-public-read' in writes[0][0]

