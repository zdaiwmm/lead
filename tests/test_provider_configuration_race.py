"""Independent provider authority and publication race probes; synthetic providers and isolated DB only."""
import pytest
from lead_app import admin_config, admin_settings
from lead_app import research_providers as providers
from lead_app.db import one, tx
from test_research_providers import claim, setup_config, task


@pytest.mark.asyncio
@pytest.mark.parametrize("change",["disable","reduce_limit","credential"])
async def test_replaced_provider_config_is_rejected_before_request(growth_agent,monkeypatch,change):
    setup_config(monkeypatch,10000)
    tid=task(growth_agent)
    original_args=providers._args
    def revoke_after_config(provider, operation, args):
        payload=original_args(provider,operation,args)
        if change=='credential':
            monkeypatch.setenv('TAVILY_API_KEY','fixture-rotated-credential')
        else:
            with tx() as c:
                settings,_=admin_settings.effective(c,'models')
                if change=='disable':
                    settings['research_providers']['tavily']['enabled']=False
                else:
                    settings['research_providers']['tavily'].update(limit_units=1000,project_limit_units=1000,question_limit_units=1000)
                key='models/default'
                old=admin_config.read(c,key)
                admin_config.write(c,key,settings,old['version'])
        return payload
    monkeypatch.setattr(providers,'_args',revoke_after_config)
    calls=[]
    class Response:
        status_code=200
        headers={}
        def json(self):
            return {'results':[],'usage':{'credits':1}}
    class Client:
        def __init__(self,**kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self,*args):
            pass
        async def request(self,*args,**kwargs):
            calls.append(args)
            return Response()
    monkeypatch.setattr(providers.httpx,'AsyncClient',Client)
    result=await providers.research_call(growth_agent,tid,'tavily','web.search',{'query':'community'},'revoked-config',execution_claim=claim(growth_agent,tid))
    assert calls==[]
    assert result['reason']==('not_enabled' if change=='disable' else 'provider_configuration_changed')
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.usage_events WHERE category='research_provider'")['n']==0



def test_global_and_project_model_publication_are_serialized_without_deadlock(growth_agent,monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import psycopg
    from lead_app import admin_operations
    from lead_app.config import database_kwargs
    aid=growth_agent
    with tx() as c:
        settings,_=admin_settings.effective(c,'models')
        key='models/'+aid
        old=admin_config.read(c,key)
        admin_config.write(c,key,settings,old['version'])
    global_has_catalog=Event()
    release_global=Event()
    project_started=Event()
    original_publish=admin_operations.configuration_published
    def published(c,kind,agent):
        if kind=='models' and agent is None:
            global_has_catalog.set()
            assert release_global.wait(5)
        return original_publish(c,kind,agent)
    monkeypatch.setattr(admin_operations,'configuration_published',published)
    pids={}
    def write(key):
        with tx() as c:
            c.execute("SET LOCAL lock_timeout='5s'")
            if key=='models/'+aid:
                pids['project']=c.execute('SELECT pg_backend_pid() pid').fetchone()['pid']
                project_started.set()
            settings=admin_config.read(c,key)
            admin_config.write(c,key,settings['body'],settings['version'])
        return 'ok'
    with ThreadPoolExecutor(max_workers=2) as pool:
        global_write=pool.submit(write,'models/default')
        if not global_has_catalog.wait(5):
            global_write.result(timeout=1)
            raise AssertionError('global publication did not reach hook')
        project_write=pool.submit(write,'models/'+aid)
        assert project_started.wait(5)
        try:
            with psycopg.connect(**database_kwargs(True)) as monitor:
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    row=monitor.execute('SELECT wait_event FROM pg_stat_activity WHERE pid=%s',(pids['project'],)).fetchone()
                    if row and row[0]=='advisory':
                        break
                    time.sleep(.01)
                else:
                    raise AssertionError('Project update did not reach the serialization lock')
            with tx(aid) as c:
                c.execute('SELECT id FROM lead.agents WHERE id=%s FOR NO KEY UPDATE NOWAIT',(aid,))
        finally:
            release_global.set()
        assert [global_write.result(timeout=10),project_write.result(timeout=10)]==['ok','ok']
