"""Planner contract changes are facts without rewriting owner revisions or history."""
from lead_app import domain
from lead_app.db import emit, one, require_agent, tx
from lead_app.growth import agent
from lead_app.growth.contracts import WorkResult
from lead_app.growth.runtime import advance, consume_triggers, ensure_decision, finish
from lead_app.growth.store import objective_revision
from test_growth_runtime import new_work


def configuration_fact(aid, payload=None):
    with tx(aid) as c:
        emit(c, aid, 'supervisor.wake', payload)
        return one(c, """SELECT payload FROM lead.outbox_events WHERE event_type='growth.wake'
            AND payload->>'reason'='configuration_changed' ORDER BY occurred_at DESC,id DESC LIMIT 1""")['payload']


def complete_decision(aid, ref, fact):
    claim = advance(ref)['claim']
    frozen = agent.decision_context(aid, ref['task_id'], claim, [fact])
    assert finish(claim, WorkResult(outcome='completed', output={'commands': []}))['accepted_control']
    assert consume_triggers(aid, [fact]) is None
    return frozen


def test_loaded_planner_version_wakes_once_without_changing_owner_or_frozen_decisions(growth_agent, monkeypatch):
    original_work = new_work(growth_agent)
    with tx(growth_agent) as c:
        before = require_agent(c, growth_agent)
        old_work = one(c, 'SELECT state,result FROM lead.tasks WHERE id=%s', (original_work['task_id'],))
    monkeypatch.setattr(agent, 'PROMPT_VERSION', 'growth-before-repair')
    old_fact = configuration_fact(growth_agent)
    first = ensure_decision(growth_agent, [old_fact])
    frozen = complete_decision(growth_agent, first, old_fact)
    assert frozen['prompt_version'] == 'growth-before-repair'
    assert ensure_decision(growth_agent, [configuration_fact(growth_agent)]) is None

    # A newly loaded server contract supplies the version, never a model payload
    # or a manufactured owner preference/control revision.
    monkeypatch.setattr(agent, 'PROMPT_VERSION', 'growth-after-repair')
    new_fact = configuration_fact(growth_agent, {'prompt_version': 'caller-spoofed-version',
        'control_version': 999999, 'config_revision': 999999})
    assert new_fact == {**old_fact, 'prompt_version': 'growth-after-repair'}
    second = ensure_decision(growth_agent, [new_fact])
    assert second['task_id'] != first['task_id']
    assert complete_decision(growth_agent, second, new_fact)['prompt_version'] == 'growth-after-repair'
    assert ensure_decision(growth_agent, [configuration_fact(growth_agent)]) is None
    with tx(growth_agent) as c:
        after = require_agent(c, growth_agent)
        assert all(before[key] == after[key] for key in ('control_version', 'config_revision', 'authorization_version'))
        assert objective_revision(before) == objective_revision(after)
        assert one(c, 'SELECT state,result FROM lead.tasks WHERE id=%s', (original_work['task_id'],)) == old_work
        assert one(c, "SELECT result#>'{runtime,decision_context}' context FROM lead.tasks WHERE id=%s",
            (first['task_id'],))['context'] == frozen


def test_legacy_wake_and_growth_agent_updated_keep_their_original_behavior(growth_agent):
    legacy = domain.create_agent(runtime_engine='legacy')
    try:
        with tx(legacy) as c:
            emit(c, legacy, 'supervisor.wake', {'source': 'original'})
            assert one(c, """SELECT payload FROM lead.outbox_events WHERE event_type='supervisor.wake'
                ORDER BY occurred_at DESC,id DESC LIMIT 1""")['payload'] == {'source': 'original'}
        with tx(growth_agent) as c:
            before = one(c, 'SELECT count(*) n FROM lead.outbox_events')['n']
            emit(c, growth_agent, 'agent.updated', {'prompt_version': 'caller-cannot-create-a-wake'})
            assert one(c, 'SELECT count(*) n FROM lead.outbox_events')['n'] == before
    finally:
        domain.set_control(legacy, 'archive')
