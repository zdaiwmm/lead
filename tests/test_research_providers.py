import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from lead_app import domain
from lead_app import research_providers as providers
from lead_app.db import j, one, tx
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.store import submit_work


def setup_config(monkeypatch, limit=1000):
    start=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    config={'enabled':True,'starts_at':start.isoformat(),'ends_at':(start+timedelta(days=1)).isoformat(),
        'evidence_ref':'fixture:verified-trial','limit_units':limit,'project_limit_units':limit,
        'question_limit_units':limit,'operations':{'web.search':{'trial_eligible':True,
        'price_version':'fixture:basic','max_units':1000}}}
    monkeypatch.setenv('TAVILY_API_KEY','fixture-'+str(__import__('uuid').uuid4()))
    from lead_app import admin_config, admin_settings
    with tx() as c:
        settings=admin_settings.defaults('models')
        settings['research_providers']={'tavily':config}
        old=admin_config.read(c,'models/default')
        admin_config.write(c,'models/default',settings,old['version'])
    return providers.configuration('tavily','web.search')[0]


def task(aid):
    with tx(aid) as c:
        tid=submit_work(c,aid,TaskSpec(kind='research',purpose='demand',review_at=utcnow()+timedelta(hours=1)))['task_id']
        version=one(c,'SELECT control_version FROM lead.agents WHERE id=%s',(aid,))['control_version']
        c.execute("UPDATE lead.tasks SET state='running',result=%s WHERE id=%s",(j({'runtime':{'attempt_token':'fixture:claim','control_version':version,'lease_until':(utcnow()+timedelta(minutes=5)).isoformat()}}),tid))
        return tid


def claim(aid,tid):
    with tx(aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(tid,))
        return {'ref':{'agent_id':str(aid),'task_id':str(tid),'task_generation':row['input']['task_generation']},'attempt_token':'fixture:claim','control_version':row['result']['runtime']['control_version']}


def test_sources_usage_and_error_classes():
    data={'results':[{'url':'https://example.com/post','content':'snippet','raw_content':'original text'},
                     {'url':'https://example.com/post','content':'duplicate'}],
          'failed_results':[{'url':'https://example.com/missing','error':'unavailable'}]}
    result=providers.normalize('tavily','web.extract',data,'fixture:request')
    assert result['state']=='partial' and len(result['sources'])==1
    row=result['sources'][0]
    assert row['body']=='original text' and row['excerpt']=='snippet'
    other=providers.normalize('tikhub','social.search',{'data':{'children':[{'url':'https://example.com/post'}]}})
    assert other['sources'][0]['source_id']==row['source_id']
    assert other['sources'][0]['metrics']['score'] is None
    assert providers.normalize('tavily','web.search',{'results':[]})['state']=='empty'


@pytest.mark.asyncio
async def test_shared_quota_and_unknown_billing(growth_agent, monkeypatch):
    conf=setup_config(monkeypatch)
    other=domain.create_agent(runtime_engine='growth')
    a,b=task(growth_agent),task(other)
    payload=providers._args('tavily','web.search',{'query':'community research'})
    def reserve(aid, tid, key):
        try:
            return providers._reserve(aid,tid,'tavily','web.search',payload,key,conf,claim(aid,tid))
        except ValueError as e:
            return str(e)
    try:
        results=await asyncio.gather(asyncio.to_thread(reserve,growth_agent,a,'a'),asyncio.to_thread(reserve,other,b,'b'))
        assert results.count('shared_quota_exhausted')==1
        index=next(i for i,r in enumerate(results) if isinstance(r,tuple))
        aid,tid,key=(growth_agent,a,'a') if index==0 else (other,b,'b')
        reservation=results[index][0]
        providers._settle(aid,reservation,conf,providers._failure('tavily','web.search','transport_unknown'))
        replay=reserve(aid,tid,key)
        assert replay[0] is None and replay[1]['reason']=='transport_unknown'
        with tx(aid) as c:
            assert one(c,'SELECT reserved_units FROM lead.research_allowances')['reserved_units']==1000
            assert one(c,'SELECT count(*) n FROM lead.usage_events')['n']==0
        assert reserve(aid,tid,'new-call')=='shared_quota_exhausted'
    finally:
        domain.set_control(other,'archive')


def test_settlement_idempotent_and_units_separate(growth_agent,monkeypatch):
    conf=setup_config(monkeypatch,2000)
    tid=task(growth_agent)
    reservation,_=providers._reserve(growth_agent,tid,'tavily','web.search',{'query':'x'},'once',conf,claim(growth_agent,tid))
    result={'state':'complete','request_id':'req:one','sources':[]}
    providers._settle(growth_agent,reservation,conf,result,1000)
    providers._settle(growth_agent,reservation,conf,result,1000)
    with tx(growth_agent) as c:
        quota=one(c,'SELECT * FROM lead.research_allowances WHERE connection_ref=%s',(conf['connection'],))
        assert (quota['reserved_units'],quota['spent_units'])==(0,1000)
        row=one(c,'SELECT * FROM lead.usage_events WHERE reservation_id=%s',(reservation,))
        assert row['category']=='research_provider' and row['details']['unit']=='provider_millicredits'
    with pytest.raises(ValueError,match='call_key_conflict'):
        providers._reserve(growth_agent,tid,'tavily','web.search',{'query':'changed'},'once',conf,claim(growth_agent,tid))


async def test_no_key_or_trial_local_degradation(growth_agent, monkeypatch):
    monkeypatch.setattr(providers,'env',lambda:{})
    assert (await providers.research_call(growth_agent,'unused','tavily','web.search',{},'x'))['reason']=='missing_key'
    monkeypatch.setattr(providers,'env',lambda:{'TIKHUB_API_KEY':'fake'})
    assert (await providers.research_call(growth_agent,'unused','tikhub','social.search',{},'x'))['reason']=='not_enabled'


def test_provider_costs_never_become_model_tokens(growth_agent,monkeypatch):
    from lead_app.growth.outcomes import operating_signals
    conf=setup_config(monkeypatch,2000)
    tid=task(growth_agent)
    reservation,_=providers._reserve(growth_agent,tid,'tavily','web.search',{'query':'x'},'units',conf,claim(growth_agent,tid))
    providers._settle(growth_agent,reservation,conf,{'state':'complete'},1000)
    with tx(growth_agent) as c:
        result=operating_signals(c,utcnow())
    assert result['costs']['model_tokens']==0
    assert result['costs']['held_tokens']==0
    assert result['costs']['research_providers'][0]['spent_units']==1000


def test_admin_provider_validation(monkeypatch):
    from lead_app.admin_settings import ResearchProviderSettings
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ResearchProviderSettings(enabled=True)
    with pytest.raises(ValidationError):
        ResearchProviderSettings(enabled=False,api_key='never-store-secrets')
    with pytest.raises(ValidationError):
        ResearchProviderSettings(starts_at='2026-11-12T00:00:00')


@pytest.mark.parametrize('condition',['canceled','expired','stale_token','stale_control','expired_lease','closed_experiment','expired_experiment','no_claim'])
def test_current_execution_required_before_reserving(growth_agent,monkeypatch,condition):
    conf=setup_config(monkeypatch)
    aid=growth_agent
    tid=task(aid)
    execution=claim(aid,tid)
    with tx(aid) as c:
        if condition=='canceled':
            c.execute("UPDATE lead.tasks SET state='canceled' WHERE id=%s",(tid,))
        elif condition=='expired':
            c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{spec,review_at}',%s) WHERE id=%s",(j((utcnow()-timedelta(seconds=1)).isoformat()),tid))
        elif condition=='expired_lease':
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,lease_until}',%s) WHERE id=%s",(j((utcnow()-timedelta(seconds=1)).isoformat()),tid))
        elif condition=='stale_control':
            c.execute('UPDATE lead.agents SET control_version=control_version+2 WHERE id=%s',(aid,))
        elif condition in ('closed_experiment','expired_experiment'):
            from lead_app.growth.store import objective_revision
            agent=one(c,'SELECT * FROM lead.agents WHERE id=%s',(aid,))
            ex=one(c,"""INSERT INTO lead.growth_experiments(tenant_id,agent_id,objective_revision,question_key,variation_key,state,specification)
              VALUES(lead.ctx_tenant(),%s,%s,'fixture:q','fixture:v',%s,%s) RETURNING id""",
              (aid,objective_revision(agent),'closed' if condition=='closed_experiment' else 'active',j({'review_at':(utcnow()-timedelta(seconds=1) if condition=='expired_experiment' else utcnow()+timedelta(hours=1)).isoformat()})))
            c.execute('UPDATE lead.tasks SET experiment_id=%s WHERE id=%s',(ex['id'],tid))
    if condition=='stale_token':
        execution['attempt_token']='old-worker'
    if condition=='no_claim':
        execution=None
    with pytest.raises(ValueError):
        providers._reserve(aid,tid,'tavily','web.search',{'query':'x'},'forbidden',conf,execution)
    with tx(aid) as c:
        assert one(c,"SELECT count(*) n FROM lead.budget_reservations WHERE operation_key='research:forbidden'")['n']==0


def test_environment_credential_has_one_tenant_owner(monkeypatch):
    from uuid import uuid4

    from lead_app.config import TENANT
    from lead_app.scope import tenant_scope
    other=str(uuid4())
    monkeypatch.setattr(providers,'env',lambda:{'TAVILY_API_KEY':'fixture'})
    with tenant_scope(other):
        assert providers.configuration('tavily','web.search')[1]=='missing_key'
    monkeypatch.setattr(providers,'env',lambda:{'TAVILY_API_KEY':'fixture','TAVILY_API_KEY_'+other.replace('-','').upper():'fixture'})
    with tenant_scope(TENANT):
        assert providers.configuration('tavily','web.search')[1]=='credential_scope_conflict'
    with tenant_scope(other):
        assert providers.configuration('tavily','web.search')[1]=='credential_scope_conflict'


async def test_timeout_replay_never_reissues_request(growth_agent,monkeypatch):
    import httpx
    setup_config(monkeypatch)
    tid=task(growth_agent)
    calls=[]
    def request(req):
        calls.append(req)
        raise httpx.ReadTimeout('fixture missing response',request=req)
    def transport(**kwargs):
        assert kwargs=={'retries':0}
        return httpx.MockTransport(request)
    monkeypatch.setattr(providers.httpx,'AsyncHTTPTransport',transport)
    kwargs={'execution_claim':claim(growth_agent,tid)}
    result=await providers.research_call(growth_agent,tid,'tavily','web.search',{'query':'research'},'timeout:once',**kwargs)
    replay=await providers.research_call(growth_agent,tid,'tavily','web.search',{'query':'research'},'timeout:once',**kwargs)
    assert result['reason']=='transport_unknown' and replay['replayed']
    assert len(calls)==1


async def test_success_receipt_units_and_same_call_replay(growth_agent,monkeypatch):
    import httpx
    setup_config(monkeypatch)
    tid=task(growth_agent)
    calls=[]
    def request(req):
        calls.append(req)
        assert str(req.url)=='https://api.tavily.com/search'
        return httpx.Response(200,json={'request_id':'fixture-receipt','results':[{'url':'https://example.com/source','content':'excerpt','raw_content':'actual source'}], 'usage':{'credits':1}})
    monkeypatch.setattr(providers.httpx,'AsyncHTTPTransport',lambda **kwargs:httpx.MockTransport(request))
    kwargs={'execution_claim':claim(growth_agent,tid)}
    first=await providers.research_call(growth_agent,tid,'tavily','web.search',{'query':'research'},'success:once',**kwargs)
    second=await providers.research_call(growth_agent,tid,'tavily','web.search',{'query':'research'},'success:once',**kwargs)
    assert first['billing_state']=='known' and first['sources'][0]['body']=='actual source'
    assert second['replayed'] and len(calls)==1
    with tx(growth_agent) as c:
        assert one(c,"SELECT sum(units) n FROM lead.usage_events WHERE category='research_provider'")['n']==1000


def test_same_reddit_post_deduplicates_across_provider_urls():
    public=providers.normalize('tavily','web.search',{'results':[{'url':'https://www.reddit.com/r/fixture/comments/abc123/title/','content':'source'}]})
    social=providers.normalize('tikhub','social.search',{'data':{'children':[{'post':{'id':'t3_abc123','postTitle':'Title','score':0}}]}})
    assert public['sources'][0]['source_id']==social['sources'][0]['source_id']
    assert social['sources'][0]['metrics']['score']==0
