"""Local admission failures never turn another question into a provider outage."""
from datetime import timedelta

import pytest
from lead_app import admin_config, admin_settings, domain
from lead_app import research_providers as providers
from lead_app.db import j, one, tx, uid
from lead_app.growth import runtime, tools
from lead_app.growth.contracts import ExperimentSpec, TaskSpec, utcnow
from lead_app.growth.store import create_experiment, submit_work
from test_provider_recovery import SCOPE, root_task
from test_research_providers import claim, setup_config, task


def fresh_task(aid):
    with tx(aid) as c:
        tid=submit_work(c,aid,TaskSpec(kind='research',purpose='independent research',dedupe_key=uid(),
            review_at=utcnow()+timedelta(hours=1)))['task_id']
        version=one(c,'SELECT control_version FROM lead.agents WHERE id=%s',(aid,))['control_version']
        c.execute("UPDATE lead.tasks SET state='running',result=%s WHERE id=%s",
            (j({'runtime':{'attempt_token':'fixture:claim','control_version':version,
                'lease_until':(utcnow()+timedelta(minutes=5)).isoformat()}}),tid))
        return tid

def configure_limits(aid, project=10000, question=1000):
    with tx() as c:
        settings,_=admin_settings.effective(c,'models')
        settings['research_providers']['tavily'].update(project_limit_units=project,question_limit_units=question)
        old=admin_config.read(c,'models/default')
        admin_config.write(c,'models/default',settings,old['version'])
    return providers.configuration('tavily','web.search',aid)[0]


def attach_question(aid,tid,kind):
    with tx(aid) as c:
        exp=create_experiment(c,aid,ExperimentSpec(question_type=kind,question='A bounded '+kind+' question',
            hypothesis='An original source may answer it',learning_signal='An original quote',
            token_limit=10000,review_at=utcnow()+timedelta(hours=1)))
        c.execute('UPDATE lead.tasks SET experiment_id=%s WHERE id=%s',(exp['id'],tid))
        return exp['question_key']


def install_blocked_episode(aid,tid,scope):
    with tx(aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(tid,))
        state=row['result']['runtime']
        resource,condition=runtime._current_research_resource(c,scope)
        state.update(resource_key=resource,condition_revision=condition,failure_scope=scope)
        for _ in range(3):
            runtime._record_failure(c,row,state,'source_unavailable')
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime}',%s) WHERE id=%s",(j(state),tid))
        return one(c,'SELECT * FROM lead.tasks WHERE id=%s',(tid,)),resource,condition


@pytest.fixture
def requests(monkeypatch):
    calls=[]
    class Response:
        status_code=200
        headers={}
        def json(self):
            return {'results':[{'url':'https://example.com/source','content':'Public source'}],'usage':{'credits':1}}
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
    return calls


async def test_question_quota_does_not_block_other_question_claim(growth_agent,monkeypatch,requests):
    aid=growth_agent
    setup_config(monkeypatch,10000)
    conf=configure_limits(aid)
    root=root_task(aid)
    tid=str(root['id'])
    question_key=attach_question(aid,tid,'audience')
    reservation,_=providers._reserve(aid,tid,'tavily','web.search',{'query':'first'},'audience:first',conf,claim(aid,tid))
    providers._settle(aid,reservation,conf,{'state':'complete'},1000)
    failed=await providers.research_call(aid,tid,'tavily','web.search',{'query':'over quota'},'audience:over',execution_claim=claim(aid,tid))
    assert failed['reason']=='question_quota_exhausted'
    assert failed['failure_scope']['limiting_resource']=={
        'kind':'question','agent_id':aid,'question_key':question_key,'period_key':conf['period_key']}
    root,resource,condition=install_blocked_episode(aid,tid,failed['failure_scope'])
    other=fresh_task(aid)
    assert other != tid
    attach_question(aid,other,'distribution')
    assert runtime.check_research_resource(aid,other,SCOPE) is None
    result=await providers.research_call(aid,other,'tavily','web.search',{'query':'different question'},'distribution:first',execution_claim=claim(aid,other))
    assert result['state']=='complete' and len(requests)==1
    probe=await tools.probe_research_resource(aid,root,{**failed['failure_scope'],'resource':resource,'condition_revision':condition})
    assert probe.outcome=='failed'
    with tx(aid) as c:
        assert one(c,'SELECT count(*) n FROM lead.budget_reservations')['n']==2
        assert one(c,"SELECT count(*) n FROM lead.usage_events WHERE category='research_provider'")['n']==2


async def test_project_quota_scope_uses_original_project_and_keeps_shared_balance(growth_agent,monkeypatch,requests):
    aid=growth_agent
    setup_config(monkeypatch,10000)
    conf=configure_limits(aid,project=1000,question=1000)
    first=task(aid)
    reservation,_=providers._reserve(aid,first,'tavily','web.search',{'query':'first'},'project:first',conf,claim(aid,first))
    providers._settle(aid,reservation,conf,{'state':'complete'},1000)
    failed=await providers.research_call(aid,first,'tavily','web.search',{'query':'over quota'},'project:over',execution_claim=claim(aid,first))
    assert failed['reason']=='project_quota_exhausted'
    assert failed['failure_scope']['limiting_resource']=={'kind':'project','agent_id':aid,'period_key':conf['period_key']}
    other=domain.create_agent(runtime_engine='growth')
    try:
        later=task(other)
        result=await providers.research_call(other,later,'tavily','web.search',{'query':'other project'},'other-project:first',execution_claim=claim(other,later))
        assert result['state']=='complete' and len(requests)==1
        with tx(aid) as c:
            quota=one(c,'SELECT reserved_units,spent_units FROM lead.research_allowances WHERE connection_ref=%s AND period_key=%s',(conf['connection'],conf['period_key']))
            assert quota=={'reserved_units':0,'spent_units':2000}
    finally:
        domain.set_control(other,'archive')


@pytest.mark.parametrize('change,reason',[
    ('claim','stale_execution_claim'),('goal','objective_changed'),('product','product_changed'),('expired','task_window_ended')])
async def test_task_admission_failure_cannot_block_a_new_task(growth_agent,monkeypatch,requests,change,reason):
    aid=growth_agent
    setup_config(monkeypatch,10000)
    tid=str(root_task(aid)["id"])
    execution=claim(aid,tid)
    with tx(aid) as c:
        if change=='goal':
            c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{goal_revision}','999') WHERE id=%s",(tid,))
        elif change=='product':
            c.execute("INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,product_url,description,facts,source_refs) VALUES(lead.ctx_tenant(),%s,1,'https://example.com','New product','{}','[]')",(aid,))
        elif change=='expired':
            c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{spec,review_at}',%s) WHERE id=%s",(j((utcnow()-timedelta(seconds=1)).isoformat()),tid))
    if change=='claim':
        execution['attempt_token']='delayed-worker'
    failed=await providers.research_call(aid,tid,'tavily','web.search',{'query':'first'},'invalid-task:first',execution_claim=execution)
    assert failed['reason']==reason and requests==[]
    assert failed['failure_scope']['limiting_resource']=={'kind':'task','agent_id':aid,'task_id':tid,'task_generation':1}
    if change!='expired':
        install_blocked_episode(aid,tid,failed['failure_scope'])
    later=fresh_task(aid)
    assert later != tid
    assert runtime.check_research_resource(aid,later,SCOPE) is None
    result=await providers.research_call(aid,later,'tavily','web.search',{'query':'new task'},'new-task:first',execution_claim=claim(aid,later))
    assert result['state']=='complete' and len(requests)==1
    with tx(aid) as c:
        assert one(c,'SELECT count(*) n FROM lead.budget_reservations')['n']==1


async def test_model_arguments_cannot_choose_the_limiting_identity(growth_agent,monkeypatch,requests):
    aid=growth_agent
    setup_config(monkeypatch,10000)
    tid=task(aid)
    result=await providers.research_call(aid,tid,'tavily','web.search',{
        'query':'first','limiting_resource':{'kind':'question','question_key':'attacker'}},'invalid-args',execution_claim=claim(aid,tid))
    assert result['reason']=='invalid_search' and requests==[]
    assert result['failure_scope']['limiting_resource']=={'kind':'task','agent_id':aid,'task_id':tid,'task_generation':1}


async def test_shared_quota_is_connection_scoped_without_new_reservation(growth_agent,monkeypatch,requests):
    aid=growth_agent
    conf=setup_config(monkeypatch,1000)
    tid=task(aid)
    reservation,_=providers._reserve(aid,tid,'tavily','web.search',{'query':'first'},'shared:first',conf,claim(aid,tid))
    providers._settle(aid,reservation,conf,{'state':'complete'},1000)
    result=await providers.research_call(aid,tid,'tavily','web.search',{'query':'over'},'shared:over',execution_claim=claim(aid,tid))
    assert result['reason']=='shared_quota_exhausted' and requests==[]
    assert result['failure_scope']['limiting_resource']=={'kind':'connection','tenant_id':str(providers.current_tenant()),
        'connection_ref':conf['connection'],'period_key':conf['period_key']}
    with tx(aid) as c:
        assert one(c,'SELECT count(*) n FROM lead.budget_reservations')['n']==1


def test_remote_failure_resource_key_retains_its_previous_contract():
    from urllib.parse import urlsplit

    from lead_app.growth.store import digest
    scope=providers._failure('tavily','web.search','rate_limited')['failure_scope']
    key=['tavily','web.search',scope['endpoint'],'provider_public','']
    assert urlsplit(scope['endpoint']).hostname=='api.tavily.com'
    assert runtime.research_resource(scope)==('research:'+digest(key),digest([key,'initial']))
