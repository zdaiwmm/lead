from datetime import timedelta
from uuid import uuid4

from lead_app.db import j, one, tx
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    utcnow,
)
from lead_app.growth.store import accept_commands, create_experiment


def test_partial_acceptance(growth_agent):
    with tx(growth_agent) as c:
        spec = ExperimentSpec(question_type="audience",question="Who needs this product?",hypothesis="A concrete audience needs it",
            learning_signal="Observed demand",token_limit=1000,review_at=utcnow()+timedelta(hours=1))
        good_exp = create_experiment(c,growth_agent,spec)
        bad_exp = create_experiment(c,growth_agent,spec.model_copy(update={"question_type":"distribution"}))
    good = TaskSpec(kind="research", purpose="product", payload={"question": "Read evidence"},
                    experiment_id=good_exp["id"],review_at=utcnow() + timedelta(hours=1))
    proposal = DecisionProposal(context_key="snapshot", rationale="Independent work must survive account changes",
        commands=[
            Command(command_key="bad", group_key="A", kind="task.create", goal_ref={'revision':1},account_id=uuid4(),
                    expected_binding=1, payload=good.model_copy(update={"experiment_id":bad_exp["id"]}).model_dump(mode="json")),
            Command(command_key="good", group_key="B", kind="task.create", goal_ref={'revision':1},payload=good.model_dump(mode="json")),
        ])
    first = accept_commands(growth_agent, proposal)
    second = accept_commands(growth_agent, proposal)
    assert len(first["accepted"]) == len(first["rejected"]) == 1
    assert first["accepted"] == second["accepted"]
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.research'")["n"] == 1


def test_new_product_context_keeps_goal_when_events_are_missing(growth_agent):
    from lead_app.growth.agent import build_context
    with tx(growth_agent) as c:
        c.execute("""INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,product_url,description,facts,source_refs)
            VALUES(lead.ctx_tenant(),%s,1,'https://example.com','A quote builder',%s,'[]')""",
            (growth_agent, j({"name": "Quote", "audience": "Independent makers"})))
    context = build_context(growth_agent)
    assert context["product"]["description"] == "A quote builder"
    assert context["measurement"]["state"] == "missing"
    assert context["goal"]["objective"] != "clicks"
    assert "cookie" not in str(context).lower()


async def test_invalid_proposal_exit(growth_agent, monkeypatch):
    from lead_app.growth import agent
    calls = []
    async def invalid(*args, **kwargs):
        calls.append(kwargs.get("call_key"))
        return {"commands":[{"kind":"invented"}]}
    monkeypatch.setattr(agent,"run_model",invalid)
    import pytest
    with pytest.raises(ValueError,match="invalid_proposal"):
        await agent.decide(growth_agent)
    assert calls == ["primary","repair"]


async def test_structured_repair_receives_exact_validation_error_without_source_input(growth_agent,monkeypatch):
    from lead_app.growth import agent
    from pydantic import ValidationError
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    calls=[]
    async def provider(schema,context,**kwargs):
        calls.append(context)
        if len(calls)==1:
            try:
                DecisionProposal.model_validate_json('{"untrusted_source":"fixture-text",}')
            except ValidationError as error:
                raise UnexpectedModelBehavior('Exceeded maximum output retries (0)') from error
        return DecisionProposal(context_key=context['context_key'],rationale='No new action is needed.',commands=[])
    monkeypatch.setattr(agent,'run_model',provider)
    result=await agent.decide(growth_agent)
    assert result['accepted']==[] and len(calls)==2
    repair=calls[1]['repair']
    assert repair['errors'][0]['type']=='json_invalid'
    assert 'input' not in repair['errors'][0] and 'fixture-text' not in str(repair)


async def test_group_repair_receives_committed_receipts_and_preserves_atomic_rejected_work(growth_agent, monkeypatch):
    from lead_app.growth import agent
    calls = []
    deadline = utcnow() + timedelta(hours=1)

    def group(key, queries):
        experiment = ExperimentSpec(question_type='audience', question='Who needs ' + key + '?',
            hypothesis='Public evidence may distinguish a specific audience', learning_signal='A sourced request',
            audience=key, token_limit=1000, review_at=deadline)
        task = TaskSpec(kind='research', purpose='Check ' + key, review_at=deadline,
            payload={'question': 'Find specific public requests', 'queries': queries})
        return [Command(command_key=key + '-experiment', group_key=key, kind='experiment.create',
                        goal_ref={'revision': 1}, payload=experiment.model_dump(mode='json')),
                Command(command_key=key + '-research', group_key=key, kind='task.create',
                        goal_ref={'revision': 1}, payload=task.model_dump(mode='json'))]

    async def provider(schema, context, **kwargs):
        calls.append(context)
        if len(calls) == 1:
            commands = group('healthy', ['one']) + group('rejected', ['one', 'two', 'three'])
        else:
            repair = context['repair']
            assert repair['groups'] == ['rejected']
            assert len(repair['accepted']) == 1
            receipt = repair['accepted'][0]
            assert receipt['group'] == 'healthy' and len(receipt['task_refs']) == 1
            with tx(growth_agent) as c:
                assert one(c, 'SELECT id FROM lead.growth_experiments WHERE id=%s', (receipt['experiment_id'],))
                assert one(c, 'SELECT count(*) n FROM lead.growth_experiments')["n"] == 1
            commands = group('rejected', ['one', 'two'])
        return DecisionProposal(context_key=context['context_key'], rationale='Repair the complete rejected group only',
                                commands=commands)

    monkeypatch.setattr(agent, 'run_model', provider)
    result = await agent.decide(growth_agent)
    assert len(calls) == 2 and not result['rejected']
    assert len(result['accepted']) == 2
    with tx(growth_agent) as c:
        assert one(c, 'SELECT count(*) n FROM lead.growth_experiments')['n'] == 2
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.research'")['n'] == 2


def test_product_changed_while_model_worked_rejects_stale_business_input(growth_agent):
    from lead_app.growth.runtime import advance
    from test_growth_runtime import new_work
    original=new_work(growth_agent)
    with tx(growth_agent) as c:
        c.execute("""INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs)
            VALUES(lead.ctx_tenant(),%s,1,'A newly supplied different product','{}','[]')""",(growth_agent,))
    assert advance(original)["reason"]=="product_changed"
    proposal=DecisionProposal(context_key="captured",rationale="Read the earlier product",commands=[
        Command(command_key="exp",group_key="exp",kind="experiment.create",goal_ref={'revision':1},payload=ExperimentSpec(
            question_type="audience",question="Who needs the old product?",hypothesis="Makers need it",
            learning_signal="A real request",token_limit=1000,review_at=utcnow()+timedelta(hours=1)).model_dump(mode="json"))])
    result=accept_commands(growth_agent,proposal,business_snapshot={"id":None})
    assert not result["accepted"] and result["rejected"][0]["reason"]=="product_changed"
