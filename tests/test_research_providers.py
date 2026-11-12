import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from lead_app import domain
from lead_app import research_providers as providers
from lead_app.db import one, tx
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.store import submit_work


def setup_config(monkeypatch, limit=1000):
    start=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    config={'starts_at':start.isoformat(),'ends_at':(start+timedelta(days=1)).isoformat(),
        'evidence_ref':'fixture:verified-trial','limit_units':limit,'project_limit_units':limit,
        'question_limit_units':limit,'operations':{'web.search':{'trial_eligible':True,
        'price_version':'fixture:basic','max_units':1000}}}
    monkeypatch.setenv('TAVILY_API_KEY','fixture-'+str(__import__('uuid').uuid4()))
    monkeypatch.setenv('LEAD_RESEARCH_ALLOWANCES',json.dumps({'tavily':config}))
    return providers.configuration('tavily','web.search')[0]


def task(aid):
    with tx(aid) as c:
        return submit_work(c,aid,TaskSpec(kind='research',purpose='demand',review_at=utcnow()+timedelta(hours=1)))['task_id']


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
            return providers._reserve(aid,tid,'tavily','web.search',payload,key,conf)
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
    reservation,_=providers._reserve(growth_agent,tid,'tavily','web.search',{'query':'x'},'once',conf)
    result={'state':'complete','request_id':'req:one','sources':[]}
    providers._settle(growth_agent,reservation,conf,result,1000)
    providers._settle(growth_agent,reservation,conf,result,1000)
    with tx(growth_agent) as c:
        quota=one(c,'SELECT * FROM lead.research_allowances WHERE connection_ref=%s',(conf['connection'],))
        assert (quota['reserved_units'],quota['spent_units'])==(0,1000)
        row=one(c,'SELECT * FROM lead.usage_events WHERE reservation_id=%s',(reservation,))
        assert row['category']=='research_provider' and row['details']['unit']=='provider_millicredits'
    with pytest.raises(ValueError,match='call_key_conflict'):
        providers._reserve(growth_agent,tid,'tavily','web.search',{'query':'changed'},'once',conf)


async def test_no_key_or_trial_local_degradation(growth_agent, monkeypatch):
    monkeypatch.setattr(providers,'env',lambda:{})
    assert (await providers.research_call(growth_agent,'unused','tavily','web.search',{},'x'))['reason']=='missing_key'
    monkeypatch.setattr(providers,'env',lambda:{'TIKHUB_API_KEY':'fake'})
    assert (await providers.research_call(growth_agent,'unused','tikhub','social.search',{},'x'))['reason']=='eligibility_unverified'
