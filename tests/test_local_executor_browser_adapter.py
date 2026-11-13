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
    ({'kind':'model','browser_run_id':'run','requires_advanced_research':True},{'public_web':True,'community_research':True,'advanced_research':False}),
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


def test_original_codex_task_keeps_native_route_after_project_mode_change(monkeypatch):
    from lead_app import local_executor, local_executor_client, native_runner

    calls=[]
    def uses_native(aid,*,task_id=None):
        calls.append((aid,task_id))
        return task_id=='original-native-task'
    monkeypatch.setattr(local_executor_client,'uses_native',uses_native,raising=False)
    monkeypatch.setattr(local_executor,'enabled',lambda *args:False)
    assert native_runner.local_executor_enabled('a',task_id='original-native-task')
    assert not native_runner.local_executor_enabled('a',task_id='new-legacy-task')
    assert calls==[('a','original-native-task'),('a','new-legacy-task')]


@pytest.mark.asyncio
async def test_basic_public_read_evidence_does_not_require_advanced_research(monkeypatch):
    from lead_app import admin_settings, fetch, local_executor_tools

    monkeypatch.setattr(admin_settings,'for_agent',lambda *args:({'tools':{
        'public_web':True,'community_research':False,'advanced_research':False}},'current'))
    read=AsyncMock(return_value={'url':'https://example.com/','title':'Actual HTTP source','text':'actual text'})
    monkeypatch.setattr(fetch,'fetch_product',read)
    monkeypatch.setattr(local_executor_tools,'_record_public_read',lambda request,context,value:value)
    device={'id':'d','agent_id':'a','tenant_id':'t'}
    request={'id':'r','device_id':'d','agent_id':'a','tenant_id':'t','payload':{
        'tool_context':{'kind':'model','browser_run_id':'basic-run'}}}
    observed=await local_executor_tools.call(device,request,'lead_public_read',{'url':'https://example.com/'})
    assert observed['structuredContent']['text']=='actual text'
    read.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_connection_claim_continues_original_thread_without_credential_resubmission(monkeypatch):
    from contextlib import contextmanager

    from lead_app import db, local_executor

    submitted=[]
    @contextmanager
    def transaction(*args):
        yield SimpleNamespace(execute=lambda *args:None)
    monkeypatch.setattr(db,'tx',transaction)
    monkeypatch.setattr(local_executor,'submit',lambda aid,key,payload,**kw:submitted.append((key,payload,kw)) or {'id':'continued-request'})
    async def status(*args):
        return {'state':'completed','result':{'output':'identity needs correction'}}
    monkeypatch.setattr(adapter,'_request_status',status)
    task={'input':{'task_generation':2},'result':{'runtime':{'attempt_token':'new-original-claim'}}}
    controller=SimpleNamespace(actor=SimpleNamespace(aid='a',profile={'id':'p'}),task_id='original-task',
        local_executor_request_id='old-request',local_executor_context={'attempt_token':'old-original-claim'},
        local_executor_worker=None,lease={'control_version':7,'authentication_dispatched':True},
        self_entry={'url':'https://fixture.test/private','acting_subject':'current.handle'},
        start=AsyncMock(),snapshot=lambda:(task,{},{}),person_only_challenge=AsyncMock(return_value=False),
        observe_current=AsyncMock(return_value=None),credentials=AsyncMock(side_effect=AssertionError('do not unseal or resend credentials')),
        page=SimpleNamespace(url='https://fixture.test/private'))
    result=await adapter.run(controller,'https://gateway','connection','Verify original account')
    assert result['executor_pending'] and result['executor_request_id']=='continued-request'
    _,payload,_=submitted[0]
    assert payload['session_key']=='browser:original-task:2'
    assert payload['tool_context']['attempt_token']=='new-original-claim'
    assert payload['tool_context']['auth_verification_only'] is True
    assert 'browser_input' not in {tool['name'] for tool in payload['tools']}
    assert 'already submitted' in payload['input']
    controller.credentials.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_subject_mismatch_has_fixed_actionable_feedback_without_weakening_proof(monkeypatch):
    from lead_app import native_identity_facts
    from lead_app.native_connection import NativeConnection
    from lead_app.native_identity_errors import IdentityFactError, identity_rpc_feedback

    page=SimpleNamespace(url='https://fixture.test/private')
    runtime=SimpleNamespace(acquire_page=AsyncMock(return_value={'target_id':'fresh'}),
        pages={'fresh':{'page':page}},release_page=AsyncMock())
    controller=NativeConnection.__new__(NativeConnection)
    controller.actor=SimpleNamespace(aid='a',session='original-browser',native_runtime=runtime)
    controller.connection_id='original-connection'
    controller.page=page
    controller.self_entry={'url':page.url,'acting_subject':'current.handle'}
    controller.lease={'target_id':'original-page','authentication_dispatched':True}
    controller.descriptor=AsyncMock()
    controller.snapshot=lambda:({}, {'challenge_version':1,'input_seq':1}, {'binding_version':1})
    observed={'subject':'different-stable-id','value_prefix':'','value_suffix':'','evidence':{'label':'Account ID'}}
    monkeypatch.setattr(native_identity_facts,'inspect_private_subject',AsyncMock(return_value=observed))
    with pytest.raises(IdentityFactError) as caught:
        await controller.report_identity({'subject_selector':'#actually-observed-id'})
    assert caught.value.code=='identity_private_current_subject_mismatch'
    feedback=identity_rpc_feedback('report_connection',409,{'code':caught.value.code})
    assert 'current' in feedback and 'private' in feedback
    assert 'different-stable-id' not in feedback
    runtime.release_page.assert_awaited_once()


@pytest.mark.asyncio
async def test_matching_private_subject_still_requires_fresh_second_source(monkeypatch):
    from contextlib import contextmanager

    from lead_app import native_connection, native_identity_facts
    from lead_app.native_connection import NativeConnection

    writes=[]
    @contextmanager
    def transaction(*args):
        yield SimpleNamespace(execute=lambda *args:writes.append(args))
    monkeypatch.setattr(native_connection,'tx',transaction)
    page=SimpleNamespace(url='https://fixture.test/private')
    fresh=SimpleNamespace(url=page.url)
    runtime=SimpleNamespace(acquire_page=AsyncMock(return_value={'target_id':'fresh'}),
        pages={'fresh':{'page':fresh}},release_page=AsyncMock())
    controller=NativeConnection.__new__(NativeConnection)
    controller.actor=SimpleNamespace(aid='a',session='original-browser',native_runtime=runtime)
    controller.connection_id='original-connection'
    controller.page=page
    controller.self_entry={'url':page.url,'selector':'a[rel=me]','acting_subject':'current.handle'}
    controller.lease={'target_id':'original','challenge_version':1,'authentication_dispatched':True}
    controller.descriptor=AsyncMock()
    controller.snapshot=lambda:({}, {'challenge_version':1,'input_seq':1}, {'binding_version':1,'platform':'web'})
    controller.remember_observation=lambda observed,expected:observed
    observed={'subject':'current.handle','value_prefix':'','value_suffix':'','evidence':{'label':'Username'}}
    inspect=AsyncMock(return_value=observed)
    monkeypatch.setattr(native_identity_facts,'inspect_private_subject',inspect)
    value=await controller.report_identity({'subject_selector':'#observed-current-handle'})
    assert value['status']=='authenticated'
    assert value['operating_subject']=={'namespace':'web','external_id':'current.handle','id_kind':'handle'}
    assert inspect.await_count==2
    assert inspect.await_args_list[0].args[0] is page
    assert inspect.await_args_list[1].args[0] is fresh
    assert len(writes)==1


@pytest.mark.asyncio
async def test_inspect_self_returns_server_observed_subject_and_namespace(monkeypatch):
    from lead_app import native_identity_facts
    from lead_app.native_connection import NativeConnection

    controller=NativeConnection.__new__(NativeConnection)
    controller.page=SimpleNamespace(url='https://fixture.test/home',goto=AsyncMock())
    controller.descriptor=AsyncMock()
    controller.snapshot=lambda:({}, {}, {'connection_origin':'https://fixture.test','platform':'web'})
    controller.page.goto.side_effect=lambda url,**kwargs:setattr(controller.page,'url',url)
    monkeypatch.setattr(native_identity_facts,'inspect_current_identity',AsyncMock(return_value={
        'self_url':'https://fixture.test/private','self_link_selector':'a[rel=me]','acting_subject':'current.handle',
        'acting_subject_selector':'#current','self_subject_prefix':'Signed in as ','self_subject_suffix':''}))
    observed=await controller.inspect_self({})
    assert observed['acting_subject']=='current.handle'
    assert observed['namespace']=='web'
    assert controller.self_entry['acting_subject']==observed['acting_subject']


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

