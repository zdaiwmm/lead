"""Fixed 45be17b review probes; synthetic providers and isolated DB only."""
import pytest
from lead_app import admin_config, admin_settings
from lead_app import research_providers as providers
from lead_app.db import one, tx
from test_research_providers import claim, setup_config, task


@pytest.mark.asyncio
async def test_provider_disabled_during_argument_preparation_cannot_reserve_or_send(growth_agent,monkeypatch):
    setup_config(monkeypatch,10000)
    tid=task(growth_agent)
    original_args=providers._args
    def revoke_after_config(provider, operation, args):
        payload=original_args(provider,operation,args)
        with tx() as c:
            settings,_=admin_settings.effective(c,'models')
            settings['research_providers']['tavily']['enabled']=False
            key='models/default'
            old=admin_config.read(c,key)
            admin_config.write(c,key,settings,old['version'])
        assert providers.configuration('tavily','web.search',growth_agent)[1]=='not_enabled'
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
    assert calls == []
    assert result['reason'] == 'not_enabled'
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.usage_events WHERE category='research_provider'")['n']==0
