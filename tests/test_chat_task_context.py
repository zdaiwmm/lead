"""Chat references retained work without embedding previous decision contexts."""
import json

import pytest
from lead_app import activities, domain, local_executor_tools
from lead_app.db import j, one, tx, uid
from lead_app.model import Decision


def retain_task(c, aid, kind, state, task_input, result):
    return one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,
        scheduled_at,completed_at) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,now(),now())
        RETURNING id,type,state,created_at,scheduled_at,completed_at""",
        (aid, kind, state, uid(), j(task_input), j(result)))


@pytest.mark.parametrize('engine', ['growth', 'legacy'])
async def test_chat_references_thirty_large_tasks_without_copying_the_ledger(monkeypatch, engine):
    monkeypatch.setattr(domain, 'emit', lambda *args, **kwargs: None)
    aid = domain.create_agent(runtime_engine=engine)
    task_input = {'spec': {'kind': 'decision', 'purpose': 'retained-decision',
        'payload': {'brief': 'ORIGINAL_TASK_INPUT_' + 'i' * 20000}}}
    result = {'runtime': {'decision_context': {'tasks': [
        {'input': 'FROZEN_DECISION_CONTEXT_' + 'c' * 40000}]}},
        'output': {'outcome': 'completed', 'output': {'detail': 'ORIGINAL_TASK_OUTPUT_' + 'o' * 20000}}}
    with tx(aid) as c:
        expected = [retain_task(c, aid, 'growth.decision', 'succeeded' if i % 2 else 'failed',
            task_input, result) for i in range(30)]
    original = domain.agent_detail(aid, chat=True)
    assert len(original['tasks']) == 30
    assert len(json.dumps(original['tasks'], default=str)) > 1048576
    calls = []

    async def answer(output, prompt, **kwargs):
        calls.append(prompt)
        assert output is Decision and kwargs['agent_id'] == aid
        return Decision(reply='已核对当前任务。')

    monkeypatch.setattr(activities, 'run_model', answer)
    message = domain.submit_message(aid, '请核对当前任务进展', uid())
    with tx(aid) as c:
        job = one(c, 'SELECT id FROM lead.chat_jobs WHERE message_id=%s', (message['id'],))
    args = {'agent_id': aid, 'job_id': str(job['id'])}
    await activities._chat_job(args)
    prompt = calls[0]
    tasks = prompt['server_context']['tasks']
    assert {str(task['id']): task for task in tasks} == {
        str(task['id']): task for task in expected}
    assert len(json.dumps(tasks, default=str)) < 20000
    encoded = json.dumps(prompt, default=str)
    for marker in ('ORIGINAL_TASK_INPUT_', 'FROZEN_DECISION_CONTEXT_', 'ORIGINAL_TASK_OUTPUT_'):
        assert marker not in encoded
    assert (prompt['server_context']['growth'] is not None) == (engine == 'growth')
    # The model can resolve the unchanged ID with the existing scoped reader.
    for task in (tasks[0], tasks[-1]):
        retained = local_executor_tools._business_read({'agent_id': aid}, 'lead_task_read', task['id'])
        assert retained['state'] == task['state']
        assert retained['specification'] == task_input['spec']
        assert retained['output'] == result['output']
    await activities._chat_job(args)
    assert len(calls) == 1
    after = domain.agent_detail(aid, chat=True)
    assert after['tasks'] == original['tasks']
    assert after['messages'][-1]['content']['text'] == '已核对当前任务。'
    with tx(aid) as c:
        assert one(c, 'SELECT status,error_code FROM lead.chat_jobs WHERE id=%s', (job['id'],)) == {
            'status': 'complete', 'error_code': None}


async def test_chat_keeps_legacy_operating_decisions_out_of_task_context(monkeypatch):
    monkeypatch.setattr(domain, 'emit', lambda *args, **kwargs: None)
    aid = domain.create_agent()
    with tx(aid) as c:
        retain_task(c, aid, 'operating.decision', 'succeeded', {}, {})

    async def answer(output, prompt, **kwargs):
        assert prompt['server_context']['tasks'] == []
        return Decision(reply='已核对。')

    monkeypatch.setattr(activities, 'run_model', answer)
    message = domain.submit_message(aid, '请核对当前任务进展', uid())
    with tx(aid) as c:
        job = one(c, 'SELECT id FROM lead.chat_jobs WHERE message_id=%s', (message['id'],))
    await activities._chat_job({'agent_id': aid, 'job_id': str(job['id'])})
    assert domain.agent_detail(aid)['messages'][-1]['content']['text'] == '已核对。'
