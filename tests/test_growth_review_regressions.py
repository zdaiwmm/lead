import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from lead_app import domain, model
from lead_app.account_intents import set_intent
from lead_app.budget import growth_cost_context
from lead_app.db import one, tx
from lead_app.growth import agent
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    WorkResult,
    utcnow,
)
from lead_app.growth.runtime import (
    advance,
    consume_triggers,
    ensure_decision,
    finish,
    recover_due,
)
from pydantic_ai.usage import RunUsage
from test_growth_account_connection import account, login_work
from test_growth_runtime import new_work


def proposal(context):
    return DecisionProposal(context_key=context['context_key'],rationale='A bounded question from supplied facts',commands=[
        Command(command_key='experiment',group_key='experiment',kind='experiment.create',goal_ref={'revision':context['goal']['revision']},payload=ExperimentSpec(
            question_type='audience',question='Who needs this product?',hypothesis='An audience needs it',
            learning_signal='A relevant source-backed request',token_limit=1000,review_at=utcnow()+timedelta(minutes=30)).model_dump(mode='json'))])


@pytest.mark.parametrize('change',['pause_resume','expired','reclaimed'])
async def test_delayed_decision_cannot_mutate_business_after_losing_ownership(growth_agent,monkeypatch,change):
    new_work(growth_agent)
    ref=ensure_decision(growth_agent,[{'reason':'owner_facts','revision':1}])
    claim=advance(ref)['claim']
    entered,release=asyncio.Event(),asyncio.Event()
    calls=[]
    async def delayed(output,context,**kwargs):
        calls.append(1)
        entered.set()
        await release.wait()
        return proposal(context)
    monkeypatch.setattr(agent,'run_model',delayed)
    working=asyncio.create_task(agent.decide(growth_agent,ref['task_id'],decision_claim=claim))
    await asyncio.wait_for(entered.wait(),3)
    if change=='pause_resume':
        domain.set_control(growth_agent,'pause')
        domain.set_control(growth_agent,'resume')
    else:
        with tx(growth_agent) as c:
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,lease_until}',to_jsonb(%s::text)) WHERE id=%s",(
                (utcnow()-timedelta(seconds=1)).isoformat(),ref['task_id']))
        if change=='reclaimed':
            recover_due(growth_agent)
            assert advance(ref)['claim']['attempt_token']!=claim['attempt_token']
    release.set()
    result=await asyncio.wait_for(working,3)
    assert result['discarded']=='stale_decision' and not result['accepted'] and len(calls)==1
    assert not finish(claim,WorkResult(outcome='completed'))['accepted_control']
    with tx(growth_agent) as c:
        assert one(c,'SELECT count(*) n FROM lead.growth_experiments')['n']==0


def test_actual_preference_write_preserves_login_and_independent_work(growth_agent):
    new_work(growth_agent)
    sid=account(growth_agent)
    login,_,_=login_work(growth_agent,sid)
    with tx(growth_agent) as c:
        original=one(c,"SELECT id FROM lead.tasks WHERE type='growth.research'")
        domain.remember(c,growth_agent,'Use clear factual language.')
        assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(original['id'],))['state']=='planned'
        assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(login['id'],))['state']=='planned'
        assert one(c,"SELECT payload->>'reason' reason FROM lead.outbox_events WHERE event_type='growth.wake' ORDER BY occurred_at DESC LIMIT 1")['reason']=='preferences_changed'


def test_resume_after_consumed_pause_has_a_new_canonical_wake(growth_agent):
    new_work(growth_agent)
    sid=account(growth_agent)
    set_intent(growth_agent,sid,'paused',source='owner_command',source_ref='owner:pause-event')
    def event():
        with tx(growth_agent) as c:
            return one(c,"SELECT payload FROM lead.outbox_events WHERE event_type='growth.wake' ORDER BY occurred_at DESC,id DESC LIMIT 1")['payload']
    paused=event()
    first=ensure_decision(growth_agent,[paused])
    finish(advance(first)['claim'],WorkResult(outcome='completed'))
    consume_triggers(growth_agent,[paused])
    set_intent(growth_agent,sid,'running',source='owner_command',source_ref='owner:resume-event')
    resumed=event()
    assert resumed['revision']>paused['revision'] and resumed['desired']=='running'
    second=ensure_decision(growth_agent,[resumed])
    assert second and second['task_id']!=first['task_id']


async def test_settled_model_output_replays_the_original_decision_context(growth_agent,monkeypatch,local_model_settings):
    new_work(growth_agent)
    ref=ensure_decision(growth_agent,[{'reason':'owner_facts','revision':1}])
    claim=advance(ref)['claim']
    transport=[]
    async def respond(self,encoded,**kwargs):
        import json
        context=json.loads(encoded)['task']
        transport.append(context['context_key'])
        return SimpleNamespace(output=proposal(context),usage=RunUsage(input_tokens=30,output_tokens=20,requests=1))
    monkeypatch.setattr(model.Agent,'run',respond)
    token=growth_cost_context.set({'task_id':ref['task_id']})
    try:
        first=await agent.decide(growth_agent,ref['task_id'],decision_claim=claim)
        with tx(growth_agent) as c:
            saved=one(c,"SELECT result#>'{runtime,decision_context}' context FROM lead.tasks WHERE id=%s",(ref['task_id'],))['context']
        # Advancing clocks/results would create a different context if rebuilt.
        monkeypatch.setattr(agent,'build_context',lambda *args:(_ for _ in ()).throw(AssertionError('Must reuse immutable decision input')))
        second=await agent.decide(growth_agent,ref['task_id'],decision_claim=claim)
        assert first['accepted']==second['accepted'] and len(transport)==1
        assert transport[0]==saved['context_key']
        with tx(growth_agent) as c:
            assert one(c,'SELECT count(*) n FROM lead.usage_events')['n']==1
    finally:
        growth_cost_context.reset(token)


async def test_growth_deepseek_uses_structured_output_tool_at_provider_boundary(growth_agent,monkeypatch,local_model_settings):
    import json

    import httpx
    ref=new_work(growth_agent)
    calls=[]
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'id':'synthetic-json-mode','object':'chat.completion','created':1794441600,
            'model':'deepseek-flash','choices':[{'index':0,'finish_reason':'tool_calls','message':{'role':'assistant',
                'content':None,'tool_calls':[{'id':'synthetic-output','type':'function','function':{'name':calls[-1]['tools'][0]['function']['name'],
                    'arguments':json.dumps({'context_key':'captured','rationale':'No new fact requires another action.','commands':[]})}}]}}],
            'usage':{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(model,'model_http_client',lambda *args:client)
        monkeypatch.setattr(model,'selected_effort',lambda *args:'medium')
        monkeypatch.setattr(model,'model_config',lambda:{'provider':'deepseek','model':'deepseek-flash',
            'base_url':'https://api.deepseek.com','key':'fixture-only'})
        context=growth_cost_context.set({'task_id':ref['task_id']})
        try:
            result=await model.run_model(DecisionProposal,{'context_key':'captured'},agent_id=growth_agent)
        finally:
            growth_cost_context.reset(context)
    assert result.context_key=='captured' and len(calls)==1
    schema=calls[0]['tools'][0]['function']['parameters']
    assert {'context_key','rationale','commands'}<=set(schema['required'])
    assert 'response_format' not in calls[0] and calls[0]['tool_choice']=='auto'
