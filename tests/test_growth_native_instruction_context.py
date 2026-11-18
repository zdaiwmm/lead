"""Growth decisions select instruction-aware sessions without changing other work."""
from datetime import timedelta

import pytest
from lead_app import local_executor_client
from lead_app.db import one, tx
from lead_app.growth import runtime, tools
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.store import submit_work
from test_growth_runtime import new_work
from test_local_executor import connected


@pytest.mark.parametrize('kind', ['decision', 'content'])
async def test_only_growth_decision_requests_instruction_session(growth_agent, monkeypatch, kind):
    connected(growth_agent)
    new_work(growth_agent)
    with tx(growth_agent) as c:
        ref = submit_work(c, growth_agent, TaskSpec(kind=kind, purpose='session-context',
            payload={'brief': 'Explain the product from retained evidence.'} if kind == 'content' else {},
            review_at=utcnow()+timedelta(hours=1)))
    claim = runtime.advance(ref)['claim']
    observed = {}

    async def execute(_aid, _row, _claim):
        observed.update(local_executor_client.execution_context.get())
        return WorkResult(outcome='completed')

    monkeypatch.setattr(tools, 'execute_tool', execute)
    result = await runtime.execute(claim)
    assert result['outcome'] == 'completed'
    assert observed['growth_decision_session'] is (kind == 'decision')
    assert observed['task_id'] == ref['task_id']
    assert observed['session_key'].startswith('decision:' if kind == 'decision' else 'task:')
    assert local_executor_client.execution_context.get() is None


def test_reconciling_decision_remains_the_only_current_decision(growth_agent):
    new_work(growth_agent)
    first = runtime.ensure_decision(growth_agent, [{'reason': 'configuration_changed', 'config_revision': 1}])
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET state='reconciling' WHERE id=%s", (first['task_id'],))
    follow = runtime.ensure_decision(growth_agent, [{'reason': 'configuration_changed', 'config_revision': 2}])
    assert follow == first
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.decision'")['n'] == 1
