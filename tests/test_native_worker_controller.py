"""Fresh page-local SW evidence never borrows another observer or old version."""
import asyncio
import copy
from types import SimpleNamespace

import pytest


def runtime_for(*,controller=True,fresh=True,controls=True):
    version={'versionId':'v1','targetId':'worker','scriptURL':'https://site.test/sw.js',
        'status':'activated','runningStatus':'running','controlledClients':['page']}
    calls=[]
    class Page:
        url='https://site.test/home'
        async def evaluate(self,_):
            return {'script_url':'https://site.test/sw.js','state':'activated'} if controller else None
    page=Page()
    gate=SimpleNamespace(context_id='managed',token=object(),
        targets={'page-session':{'targetId':'page','type':'page','browserContextId':'managed'},
            'worker-session':{'targetId':'worker','type':'service_worker','browserContextId':'managed','url':version['scriptURL']}},
        owners={'page-session':'page'},worker_versions_by_page={'page-session':{'v1':copy.deepcopy(version)},
            'other-page':{'unchanged':{'controlledClients':['other']}}},
        worker_versions={'v1':copy.deepcopy(version)})
    gate.worker_versions_by_page['page-session']['v1']['controlledClients']=[]
    runtime=SimpleNamespace(gate=gate,closed=False,pages={'page':{'page':page,'scope':{'task_id':'original'}}})
    scope_calls=[]
    def current_scope(scope):
        scope_calls.append(scope['task_id'])
        return {'state':'running'}
    runtime.current_scope=current_scope
    frame={'id':'root-frame','loaderId':'loader','url':page.url}
    async def send(method,params,session):
        calls.append((method,session))
        assert session=='page-session'
        if method=='Page.getFrameTree':
            return {'frameTree':{'frame':copy.deepcopy(frame)}}
        if method=='ServiceWorker.enable' and fresh:
            update=copy.deepcopy(version)
            update['controlledClients']=['page'] if controls else ['another-page']
            gate.worker_versions_by_page[session]={'v1':update}
        return {}
    gate.send=send
    return runtime,calls,scope_calls,frame


@pytest.mark.asyncio
async def test_refresh_uses_new_page_projection_and_keeps_other_observer_untouched():
    from lead_app.native_worker_controller import refresh_page_controller
    runtime,calls,scope_calls,_=runtime_for()
    other=copy.deepcopy(runtime.gate.worker_versions_by_page['other-page'])
    proof=await refresh_page_controller(runtime,'page')
    assert proof['worker_target_id']=='worker' and proof['worker_version_id']=='v1'
    assert runtime.gate.worker_versions_by_page['page-session']['v1']['controlledClients']==['page']
    assert runtime.gate.worker_versions_by_page['other-page']==other
    assert [name for name,_ in calls if name.startswith('ServiceWorker.')]==['ServiceWorker.disable','ServiceWorker.enable']
    assert len(scope_calls)>=2


@pytest.mark.asyncio
async def test_no_controller_removes_old_stamp_without_failing_normal_page():
    from lead_app.native_worker_controller import refresh_page_controller
    runtime,calls,scope_calls,_=runtime_for(controller=False)
    assert await refresh_page_controller(runtime,'page') is None
    assert not runtime.gate.worker_versions_by_page.get('page-session')
    assert not any(name.startswith('ServiceWorker.') for name,_ in calls)
    assert len(scope_calls)>=2


@pytest.mark.asyncio
@pytest.mark.parametrize('fresh,controls',[(False,True),(True,False)])
async def test_old_or_global_root_cannot_replace_a_fresh_page_event(monkeypatch,fresh,controls):
    from lead_app import native_worker_controller as helper
    monkeypatch.setattr(helper,'_WAIT_SECONDS',.02)
    runtime,_,_,_=runtime_for(fresh=fresh,controls=controls)
    assert runtime.gate.worker_versions['v1']['controlledClients']==['page']
    with pytest.raises(ValueError,match='native_worker_controller_unproven'):
        await helper.refresh_page_controller(runtime,'page')


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['retired','replacement','wrong_context','duplicate_page','document','controller','scope'])
async def test_fresh_subscription_preserves_current_worker_page_document_and_claim_fences(monkeypatch,change):
    from lead_app import native_worker_controller as helper
    monkeypatch.setattr(helper,'_WAIT_SECONDS',.02)
    runtime,_,scope_calls,frame=runtime_for()
    gate=runtime.gate
    if change=='retired':
        gate.worker_versions['v1']['status']='redundant'
    elif change=='replacement':
        gate.worker_versions['v1']['targetId']='replacement'
    elif change=='wrong_context':
        gate.targets['worker-session']['browserContextId']='other'
    elif change=='duplicate_page':
        gate.targets['second-page-session']=dict(gate.targets['page-session'])
        gate.owners['second-page-session']='page'
    elif change=='document':
        original=gate.send
        trees=[0]
        async def send(method,params,session):
            if method=='Page.getFrameTree':
                trees[0]+=1
                if trees[0]>1:
                    frame['loaderId']='new-document'
            return await original(method,params,session)
        gate.send=send
    elif change=='controller':
        reads=[0]
        async def evaluate(_):
            reads[0]+=1
            return {'script_url':'https://site.test/sw.js' if reads[0]==1 else 'https://site.test/changed.js','state':'activated'}
        runtime.pages['page']['page'].evaluate=evaluate
    else:
        def current_scope(scope):
            scope_calls.append(scope['task_id'])
            if len(scope_calls)>1:
                raise ValueError('original_claim_revoked')
        runtime.current_scope=current_scope
    with pytest.raises(ValueError):
        await helper.refresh_page_controller(runtime,'page')


@pytest.mark.asyncio
async def test_different_pages_refresh_independently_without_global_serialization():
    from lead_app.native_worker_controller import refresh_page_controller
    runtime,_,_,_=runtime_for()
    gate=runtime.gate
    runtime.pages['page-two']={'page':copy.copy(runtime.pages['page']['page']),'scope':{'task_id':'other'}}
    gate.targets['page-session-two']=dict(gate.targets['page-session'],targetId='page-two')
    gate.owners['page-session-two']='page-two'
    entered=asyncio.Event()
    count=[0]
    async def send(method,params,session):
        root=gate.targets[session]['targetId']
        if method=='Page.getFrameTree':
            return {'frameTree':{'frame':{'id':root,'loaderId':'loader','url':runtime.pages[root]['page'].url}}}
        if method=='ServiceWorker.disable':
            count[0]+=1
            if count[0]==2:
                entered.set()
            await asyncio.wait_for(entered.wait(),.3)
        if method=='ServiceWorker.enable':
            version=copy.deepcopy(gate.worker_versions['v1'])
            version['controlledClients']=[root]
            gate.worker_versions_by_page[session]={'v1':version}
        return {}
    gate.send=send
    result=await asyncio.gather(*(refresh_page_controller(runtime,root) for root in ('page','page-two')))
    assert len(result)==2 and count[0]==2
    assert {proof['page_target_id'] for proof in result}=={'page','page-two'}
