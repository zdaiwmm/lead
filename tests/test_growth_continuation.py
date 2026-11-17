from datetime import timedelta

import pytest
from lead_app.db import j, one, tx
from lead_app.growth import agent
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    utcnow,
)
from lead_app.growth.runtime import ensure_decision, recover_due
from lead_app.growth.store import create_experiment, submit_work
from lead_app.growth.views import render_growth
from test_growth_runtime import new_work

CONDITION = {'condition':'A new original audience request is retained as a document.',
    'producer':'evidence_added','next_action':'Compare the new request with the original audience hypothesis.',
    'responsibility':'product'}


async def test_stop_decision_preserves_rationale_and_real_resume_condition(growth_agent, monkeypatch):
    async def provider(schema, context, **kwargs):
        return schema(context_key=context['context_key'], rationale='Original source is blocked; no useful alternate is evidenced.',
            commands=[], continuation={'condition':'A new original audience request is retained as a document.',
                'producer':'evidence_added','next_action':'Compare the new request with the original audience hypothesis.',
                'responsibility':'product'})
    monkeypatch.setattr(agent, 'run_model', provider)
    result = await agent.decide(growth_agent)
    assert result['status'] == 'needs_follow_up'
    assert result['continuation_check']['reason'] == 'continuation_source_required'
    assert result['rationale'].startswith('Original source is blocked')
    assert result['continuation']['producer'] == 'evidence_added'
    assert 'check_at' not in result['continuation']
    with tx(growth_agent) as c:
        audit = one(c, "SELECT payload FROM lead.audit_events WHERE type='growth.decision_accepted' ORDER BY occurred_at DESC LIMIT 1")
    assert audit['payload']['rationale'] == result['rationale']


async def test_rejected_decision_is_explicitly_blocked_after_one_repair(growth_agent, monkeypatch):
    calls = []
    async def provider(schema, context, **kwargs):
        calls.append(kwargs['call_key'])
        return DecisionProposal(context_key=context['context_key'], rationale='A repeated path cannot be reopened.',
            commands=[],continuation=CONDITION)
    monkeypatch.setattr(agent, 'run_model', provider)
    monkeypatch.setattr(agent, 'accept_commands', lambda *a, **k: {'decision_task_id':None, 'accepted':[],
        'rejected':[{'group':'same-path','reason':'no_progress_path_exhausted'}]})
    result = await agent.decide(growth_agent)
    assert result['status'] == 'blocked'
    assert result['rejected'][0]['reason'] == 'no_progress_path_exhausted'
    assert result['repair_exhausted'] and calls == ['primary', 'repair']
    assert result['rationale'] == 'A repeated path cannot be reopened.'


async def test_empty_decision_without_condition_is_not_reported_as_waiting(growth_agent, monkeypatch):
    calls = []
    async def provider(schema, context, **kwargs):
        calls.append(kwargs['call_key'])
        return schema(context_key=context['context_key'],rationale='No clear next step.',commands=[])
    monkeypatch.setattr(agent,'run_model',provider)
    with pytest.raises(ValueError,match='invalid_proposal'):
        await agent.decide(growth_agent)
    assert calls == ['primary','repair']


async def test_invalid_repair_keeps_last_valid_blocker(growth_agent, monkeypatch):
    calls = []
    async def provider(schema, context, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            return {'commands':[{'kind':'invented'}]}
        return schema(context_key=context['context_key'],rationale='Original path exhausted.',commands=[],continuation=CONDITION)
    monkeypatch.setattr(agent,'run_model',provider)
    monkeypatch.setattr(agent,'accept_commands',lambda *a,**k:{'accepted':[],
        'rejected':[{'group':'same-path','reason':'no_progress_path_exhausted'}]})
    result = await agent.decide(growth_agent)
    assert result['status'] == 'blocked' and result['rationale'] == 'Original path exhausted.'


async def test_close_only_gets_one_continuation_repair_without_replaying_accepted_group(growth_agent, monkeypatch):
    with tx(growth_agent) as c:
        exp = create_experiment(c,growth_agent,ExperimentSpec(question_type='audience',question='Who needs quote comparison?',
            hypothesis='Makers may need it.',learning_signal='A relevant original request.',token_limit=1000,
            review_at=utcnow()+timedelta(hours=1)))
    calls = []
    async def provider(schema,context,**kwargs):
        calls.append(kwargs['call_key'])
        if len(calls)==2:
            assert context['repair']['continuation_only']
            return schema(context_key=context['context_key'],rationale='The path has ended.',commands=[],continuation=CONDITION)
        return schema(context_key=context['context_key'],rationale='The path has ended.',commands=[Command(
            command_key='close',group_key='old-path',kind='experiment.close',experiment_id=exp['id'],expected_revision=1,
            goal_ref={'revision':1},payload={'state':'inconclusive','reason':'No relevant demand was evidenced.'})])
    monkeypatch.setattr(agent,'run_model',provider)
    result = await agent.decide(growth_agent)
    assert calls==['primary','repair'] and len(result['accepted'])==1
    assert result['status']=='needs_follow_up' and result['continuation']
    with tx(growth_agent) as c:
        assert one(c,'SELECT revision,state FROM lead.growth_experiments WHERE id=%s',(exp['id'],))=={'revision':2,'state':'closed'}
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type LIKE 'growth.%%'")['n']==0


async def test_actual_nonempty_commands_all_rejected_do_not_imply_complete_wait(growth_agent, monkeypatch):
    calls=[]
    async def provider(schema,context,**kwargs):
        calls.append(kwargs['call_key'])
        spec=TaskSpec(kind='research',purpose='demand',payload={'queries':['original request']},review_at=utcnow()+timedelta(hours=1))
        return schema(context_key=context['context_key'],rationale='Try a new demand task.',commands=[Command(
            command_key='unbounded',group_key='missing-experiment',kind='task.create',goal_ref={'revision':1},
            payload=spec.model_dump(mode='json'))])
    monkeypatch.setattr(agent,'run_model',provider)
    result=await agent.decide(growth_agent)
    assert calls==['primary','repair'] and not result['accepted']
    assert result['rejected'][0]['reason']=='bounded_experiment_required'
    assert result['status']=='needs_follow_up' and result['continuation'] is None


def stopped_root(aid):
    ref = new_work(aid)
    episode = {'status':'blocked','reason':'recovery_probe_limit','error_code':'source_unavailable',
        'probes':8,'probes_stopped':True,'resource':'source:original', 'condition_revision':'original',
        'next_check_at':(utcnow()-timedelta(days=1)).isoformat()}
    with tx(aid) as c:
        c.execute("UPDATE lead.tasks SET state='failed',completed_at=now(),result=%s WHERE id=%s",
            (j({'runtime':{'recovery':episode,'failure_scope':{'provider':'account_browser',
                'origin':'https://www.reddit.com','access_mode':'account','condition':'access_denied'}}}),ref['task_id']))
    return ref


def test_stopped_recovery_has_no_fictitious_next_check_and_remains_visible(growth_agent):
    stopped_root(growth_agent)
    before = render_growth(growth_agent)
    waiting = before['waiting'][0]['wait']
    assert waiting['check_at'] is None
    assert waiting['automatic_checks'] == 'stopped'
    assert waiting['resume_when']
    assert before['continuation']['status'] == 'blocked'
    assert before['continuation']['responsibility'] == 'product'
    for _ in range(2):
        recover_due(growth_agent)
        assert ensure_decision(growth_agent) is None
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type IN ('growth.decision','growth.recover')")['n'] == 0


def test_old_recovery_not_lost_behind_recent_work(growth_agent):
    ref = stopped_root(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET created_at=now()-interval '3 days' WHERE id=%s",(ref['task_id'],))
    for index in range(25):
        with tx(growth_agent) as c:
            other = submit_work(c,growth_agent,TaskSpec(kind='observe',purpose='history',dedupe_key=str(index),
                review_at=utcnow()+timedelta(hours=1)))
        with tx(growth_agent) as c:
            c.execute("UPDATE lead.tasks SET state='succeeded',completed_at=now() WHERE id=%s",(other['task_id'],))
    context = agent.build_context(growth_agent)
    assert not any(str(row['id']) == ref['task_id'] for row in context['work'])
    blocker = context['blocked_resources'][0]
    assert blocker['recovery']['probes_stopped']
    assert blocker['failure_scope']['origin'] == 'https://www.reddit.com'
    assert len(context['blocked_resources']) <= 8
