from datetime import timedelta
from types import SimpleNamespace

import pytest
from lead_app import config, model
from lead_app.budget import growth_cost_context, reserve, settle
from lead_app.db import one, tx
from lead_app.growth import tools
from lead_app.growth.contracts import ExperimentSpec, TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, execute, finish, probe_episode
from lead_app.growth.store import create_experiment, submit_work
from lead_app.growth.tools import RecoverInput
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.usage import RunUsage
from test_growth_research_transport import SOURCE, research_rpc  # noqa: F401
from test_growth_runtime import new_work


def blocked_source(aid,code):
    new_work(aid)
    with tx(aid) as c:
        exp=create_experiment(c,aid,ExperimentSpec(question_type='audience',question='Who needs quote comparison?',
            hypothesis='Makers need it',learning_signal='An original request',token_limit=200000,review_at=utcnow()+timedelta(hours=1)))
        ref=submit_work(c,aid,TaskSpec(kind='research',purpose='demand',experiment_id=exp['id'],
            payload={'question':'Who needs quote comparison?','urls':['https://forum.example/topic']},review_at=utcnow()+timedelta(hours=1)))
    for n in range(3):
        finish(advance(ref,now=utcnow()+timedelta(minutes=n))['claim'],WorkResult(outcome='failed',failure_code=code))
    from lead_app.growth.runtime import recover_due
    recover_due(aid,now=utcnow()+timedelta(minutes=10))
    with tx(aid) as c:
        root=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        probe=one(c,"SELECT * FROM lead.tasks WHERE type='growth.recover' AND input#>>'{spec,payload,episode_root}'=%s",(ref['task_id'],))
    return ref,root,probe,RecoverInput.model_validate(probe['input']['spec']['payload'])


async def test_old_dns_episode_reuses_original_task_costs_and_wakes_new_learning_once(growth_agent,monkeypatch,local_model_settings,request):
    import json
    request.getfixturevalue('research_rpc')
    ref,root,probe,data=blocked_source(growth_agent,'gaierror')
    async def unavailable(*args,**kwargs):
        raise OSError('Worker transport unavailable')
    calls=[]
    async def assess(self,encoded,**kwargs):
        context=json.loads(encoded)['task']
        calls.append(1)
        return SimpleNamespace(output=tools.ResearchAssessment(findings=[{'document_id':context['sources'][0]['document_id'],
            'quote':SOURCE,'finding':'Independent makers describe this need.','relevance':'Answers the exact audience question.'}]),
            usage=RunUsage(input_tokens=30,output_tokens=20,requests=1))
    monkeypatch.setattr(tools,'fetch_product',unavailable)
    monkeypatch.setattr(model.Agent,'run',assess)
    result=await probe_episode(growth_agent,probe,data)
    assert result.output['state']=='recovered' and result.output['resumed']['task_id']==ref['task_id']
    claim=advance(result.output['resumed'])['claim']
    observed=WorkResult.model_validate(await execute(claim))
    assert not observed.output['knowledge_progress'] and observed.output['learning_progress']
    finish(claim,observed)
    with tx(growth_agent) as c:
        cost=one(c,'SELECT pricing_snapshot FROM lead.budget_reservations')['pricing_snapshot']['growth']
        assert cost['task_id']==ref['task_id'] and cost['experiment_id']==str(root['experiment_id'])
        assert cost['recovery_root']==ref['task_id'] and cost['recovery_limit']==70000
        assert one(c,'SELECT count(*) n FROM lead.usage_events')['n']==1
        saved=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(ref['task_id'],))['result']['runtime']
        assert saved['recovery']['attempts']==3 and saved['recovery']['resumed']
        assert one(c,"SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='evidence_added' AND payload->>'task_id'=%s",(ref['task_id'],))['n']==1
        duplicate=submit_work(c,growth_agent,TaskSpec.model_validate(root['input']['spec']).model_copy(update={'dedupe_key':'verify-duplicate-quote'}))
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(duplicate['task_id'],))
    # The persisted first result makes this same quote an old finding, even if read again.
    again=await tools.research(growth_agent,row,TaskSpec.model_validate(row['input']['spec']))
    assert not again.output['knowledge_progress'] and not again.output['learning_progress']
    finish(advance(duplicate)['claim'],again)
    with tx(growth_agent) as c:
        assert not one(c,"SELECT id FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='evidence_added' AND payload->>'task_id'=%s",(duplicate['task_id'],))
    assert len(calls)==2  # One settled original-task assessment, one independent duplicate observation.
    assert (await probe_episode(growth_agent,probe,data)).output['state']=='obsolete_probe'
    assert growth_cost_context.get() is None


async def test_source_probe_retains_recovery_cost_cap_and_original_experiment(growth_agent,monkeypatch):
    ref,root,probe,data=blocked_source(growth_agent,'source_unavailable')
    async def assessment_costs(*args,**kwargs):
        charged=reserve(growth_agent,'source-probe:first',40000)
        settle(growth_agent,charged,actual=40000)
        reserve(growth_agent,'source-probe:second',31000)
        pytest.fail('Recovery cap must reject the second assessment reservation')
    monkeypatch.setattr(tools,'research',assessment_costs)
    result=await probe_episode(growth_agent,probe,data)
    assert result.output['state']=='recovery_budget_exhausted'
    with tx(growth_agent) as c:
        row=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert row['result']['runtime']['recovery']['attempts']==3
        assert row['result']['runtime']['recovery']['paid_probes_stopped']
        assert one(c,'SELECT sum(units) n FROM lead.usage_events')['n']==40000
        cost=one(c,'SELECT pricing_snapshot FROM lead.budget_reservations')['pricing_snapshot']['growth']
        assert cost['experiment_id']==str(root['experiment_id']) and cost['task_id']==ref['task_id']
    assert (await probe_episode(growth_agent,probe,data)).output['state']=='recovery_budget_exhausted'
    assert growth_cost_context.get() is None


async def test_resumed_old_dns_failure_keeps_the_episode_and_remains_probeable(growth_agent,monkeypatch):
    from lead_app.growth.runtime import recover_due
    ref,root,probe,data=blocked_source(growth_agent,'gaierror')
    original=root['result']['runtime']['recovery']
    document,_=tools._remember(growth_agent,{'url':'https://forum.example/topic','title':'Source','text':SOURCE})
    async def available(*args,**kwargs):
        return WorkResult(outcome='completed',evidence_refs=[document])
    monkeypatch.setattr(tools,'research',available)
    resumed=(await probe_episode(growth_agent,probe,data)).output['resumed']
    finish(advance(resumed)['claim'],WorkResult(outcome='failed',failure_code='source_unavailable'))
    with tx(growth_agent) as c:
        episode=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(ref['task_id'],))['result']['runtime']['recovery']
        assert episode['fingerprint']==original['fingerprint'] and episode['root_task_id']==ref['task_id']
        assert episode['attempts']==4 and episode['status']=='blocked'
    recover_due(growth_agent,now=utcnow()+timedelta(minutes=11))
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type='growth.recover' AND input#>>'{spec,payload,episode_root}'=%s",(ref['task_id'],))['n']==2


async def test_research_assessment_provider_recovers_original_research(growth_agent,monkeypatch):
    new_work(growth_agent)
    monkeypatch.setattr(config,'any_model_configured',lambda:True)
    async def fetch(url,**kwargs):
        return {'url':url,'title':'Original request','text':'Independent makers need a way to compare quote line items without retyping them.','links':[]}
    async def failed(*args,**kwargs):
        raise ModelHTTPError(503,'synthetic-model',{'message':'Synthetic provider outage'})
    monkeypatch.setattr(tools,'fetch_product',fetch)
    monkeypatch.setattr(tools,'run_model',failed)
    with tx(growth_agent) as c:
        exp=create_experiment(c,growth_agent,ExperimentSpec(question_type='audience',question='Who needs quote comparison?',
            hypothesis='Makers need it',learning_signal='An original relevant request',token_limit=100000,review_at=utcnow()+timedelta(hours=1)))
        ref=submit_work(c,growth_agent,TaskSpec(kind='research',purpose='demand',experiment_id=exp['id'],
            payload={'question':'Who needs quote comparison?','urls':['https://forum.example/original']},review_at=utcnow()+timedelta(hours=1)))
    for n in range(3):
        claim=advance(ref,now=utcnow()+timedelta(minutes=n))['claim']
        result=WorkResult.model_validate(await execute(claim))
        assert result.failure_code=='ModelHTTPError'
        finish(claim,result)
    with tx(growth_agent) as c:
        root=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        episode=root['result']['runtime']['recovery']
        assert episode['resource'].startswith('model:') and episode['status']=='blocked'
    async def healthy(output,*args,**kwargs):
        return output(ready=True)
    monkeypatch.setattr(model,'run_model',healthy)
    result=await probe_episode(growth_agent,root,RecoverInput(episode_root=ref['task_id'],condition_revision=episode['condition_revision']))
    assert result.output['state']=='recovered'
    assert result.output['resumed']['task_id']==ref['task_id']
    assert advance(result.output['resumed'])['state']=='ready'
