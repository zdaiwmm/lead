"""Real DB/outbox/Temporal recovery chain with synthetic decision/assessment only.

Run only in a fresh verify_growth_runtime.py --prepare state. HTTP HTML is
controlled at fetch.pinned_get; tools, persistence, claims, leases, probe proof
and the production outbox consumer remain real. This is deterministic engineering
evidence, not an evaluation of a model or a real social website.
"""
import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.encoders import jsonable_encoder
from lead_app import fetch, worker
from lead_app.config import ROOT, STATE, database_kwargs, env
from lead_app.db import all_rows, emit, one, tx, uid
from lead_app.growth import agent, runtime, tools, views
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    utcnow,
)
from lead_app.scope import current_tenant
from lead_app.workflows import GrowthDecisionWorkflow, GrowthWorkWorkflow
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from scripts.verify_growth_runtime import read_manifest

pytestmark = pytest.mark.skipif(os.environ.get('LEAD_GROWTH_TEST_ENV') != '1',
                                reason='An explicitly isolated verification state is required')
ORIGINAL = 'https://original.recovery-fixture.test/topic'
ALTERNATIVE = 'https://alternative.recovery-fixture.test/evidence'
QUOTE = 'Independent makers compare line items before approving a supplier quote.'


class IsolatedQueueClient:
    """Only change routing; keep original delivery IDs, signals and Temporal RPCs."""
    def __init__(self, client, queue, aid):
        self.client, self.queue, self.aid = client, queue, aid
        self.ids = set()

    async def start_workflow(self, workflow, *args, **kwargs):
        assert workflow in (GrowthWorkWorkflow.run, GrowthDecisionWorkflow.run)
        assert kwargs['task_queue'] == 'lead'
        if workflow == GrowthWorkWorkflow.run:
            assert args[0]['agent_id'] == self.aid
        else:
            assert args[0] == self.aid
        kwargs['task_queue'] = self.queue
        self.ids.add(kwargs['id'])
        return await self.client.start_workflow(workflow, *args, **kwargs)

    def get_workflow_handle(self, identity):
        return self.client.get_workflow_handle(identity)


def work_rows(aid):
    with tx(aid) as connection:
        return all_rows(connection, "SELECT * FROM lead.tasks WHERE type LIKE 'growth.%%' ORDER BY created_at,id")


def purpose(rows, name):
    return next((row for row in rows if row['input']['spec']['purpose'] == name), None)


def isolated_services():
    db = database_kwargs()
    address = os.environ['LEAD_TEST_CHANNEL_TEMPORAL']
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        # The official application job owns these disposable Compose services;
        # local runs continue to require the stricter private-state manifest.
        assert os.environ.get('GITHUB_REPOSITORY') == 'zdaiwmm/lead'
        assert os.environ.get('RUNNER_OS') == 'Linux'
        assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
        assert Path(os.environ['GITHUB_WORKSPACE']).resolve() == ROOT
        assert STATE == ROOT / '.state' and (STATE / 'infra.env').is_file()
        assert env().get('LEAD_DEPLOYMENT_MODE') != 'production'
        assert db['host'] == '127.0.0.1' and db['dbname'] == 'lead' and db['port'] == 55432
        assert address == '127.0.0.1:17233'
    else:
        manifest = read_manifest(STATE.parent)
        assert db['dbname'] == 'lead_growth_test' and db['port'] == manifest['pg_port']
        assert address == '127.0.0.1:' + str(manifest['temporal_port'])
    return address


@pytest.mark.parametrize('expire_second', [False, True], ids=['both-current', 'one-expired'])
async def test_original_source_recovery_through_real_outbox_temporal_and_current_dependencies(
        growth_agent, local_model_settings, monkeypatch, expire_second):
    # Catches fake-success recovery, scope borrowing, lost dependencies and duplicate business wakes.
    address = isolated_services()
    aid, tenant, queue = growth_agent, current_tenant(), 'research-recovery-' + uid()
    source_ready = False
    probe_entered, release_probe = asyncio.Event(), asyncio.Event()
    transport_calls, decisions = [], []
    issued = set()

    async def forbidden_http(*_args, **_kwargs):
        raise AssertionError('This integration case must never send real HTTP')

    async def response(_client, url):
        assert url in (ORIGINAL, ALTERNATIVE, ORIGINAL + '/one', ORIGINAL + '/two')
        from lead_app.budget import growth_cost_context
        is_probe = bool((growth_cost_context.get() or {}).get('recovery_root'))
        transport_calls.append((url, is_probe, source_ready))
        original = urlsplit(url).hostname == 'original.recovery-fixture.test'
        if original and source_ready and is_probe:
            probe_entered.set()
            await release_probe.wait()
        if original and not source_ready:
            body = '<html><title>Access denied</title><p>Blocked by network security. This synthetic source cannot currently be read.</p></html>'
        else:
            body = '<html><title>Supplier quote evidence</title><article>' + QUOTE + \
                ' They need transparent supplier terms and an independent comparison before they make a purchasing decision.</article></html>'
        return httpx.Response(200, content=body.encode(), headers={'Content-Type': 'text/html'}, request=httpx.Request('GET', url))

    async def model(schema, context, **_kwargs):
        if schema is tools.ResearchAssessment:
            assert all(QUOTE in item['excerpt'] for item in context['sources'])
            return schema(findings=[{'document_id': context['sources'][0]['document_id'], 'quote': QUOTE,
                'finding': 'The synthetic source describes line-item comparison.',
                'relevance': 'It answers the bounded comparison-research question.'}], unresolved=[])
        assert schema is DecisionProposal
        assert 'repair' not in context, context.get('repair')
        decisions.append(context)
        goal = {'revision': context['goal']['revision']}
        commands = []
        if not context['experiments']:
            assert 'initial' not in issued
            issued.add('initial')
            commands = [Command(command_key='initial-question', group_key='initial', kind='experiment.create', goal_ref=goal,
                payload=ExperimentSpec(question_type='audience', question='What comparison need is documented?',
                    hypothesis='Independent source documents explain supplier comparison needs',
                    learning_signal='An exact quotation from a persisted source', token_limit=100000,
                    review_at=utcnow()+timedelta(hours=4)).model_dump(mode='json')),
                Command(command_key='original-read', group_key='initial', kind='task.create', goal_ref=goal,
                    payload=TaskSpec(kind='research', purpose='blocked-original', payload={'urls': [ORIGINAL],
                        'question': 'What comparison need is documented?'}, review_at=utcnow()+timedelta(hours=4)).model_dump(mode='json'))]
        elif any(item.get('reason') == 'resource_blocked' for item in context['triggers']) and 'alternative' not in issued:
            issued.add('alternative')
            experiment = context['experiments'][0]['id']
            for name, url in (('feasible-alternative', ALTERNATIVE), ('waiting-one', ORIGINAL + '/one'), ('waiting-two', ORIGINAL + '/two')):
                commands.append(Command(command_key=name, group_key='after-block', kind='task.create', goal_ref=goal,
                    experiment_id=experiment, payload=TaskSpec(kind='research', purpose=name, experiment_id=experiment,
                        payload={'urls': [url], 'question': 'What comparison need is documented?'},
                        review_at=utcnow()+ (timedelta(seconds=10) if expire_second and name == 'waiting-two' else timedelta(hours=4))).model_dump(mode='json')))
        return schema(context_key=context['context_key'], rationale='Use a feasible independent source while the original resource remains blocked.', commands=commands)

    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden_http)
    monkeypatch.setattr(fetch, 'pinned_get', response)
    monkeypatch.setattr(agent, 'run_model', model)
    monkeypatch.setattr(tools, 'run_model', model)
    client = await Client.connect(address)
    routed = IsolatedQueueClient(client, queue, aid)

    async def pump_until(predicate, seconds=35):
        async with asyncio.timeout(seconds):
            while True:
                await worker.deliver_workspace(routed, tenant, False, True)
                if predicate(work_rows(aid)):
                    return
                await asyncio.sleep(.05)

    with tx(aid) as connection:
        connection.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        emit(connection, aid, 'growth.wake', {'reason': 'integration_start', 'transition': uid()})
    try:
        async with Worker(client, task_queue=queue, workflows=[GrowthWorkWorkflow, GrowthDecisionWorkflow],
                activities=[runtime.growth_advance, runtime.growth_execute, runtime.growth_finish, runtime.growth_coordinate]):
            await pump_until(lambda rows: all(purpose(rows, name) and purpose(rows, name)['result']['runtime'].get('blocked_resources')
                for name in ('waiting-one', 'waiting-two')) and purpose(rows, 'feasible-alternative')['state'] == 'succeeded')
            rows = work_rows(aid)
            root, alternative = purpose(rows, 'blocked-original'), purpose(rows, 'feasible-alternative')
            root_id = str(root['id'])
            episode = root['result']['runtime']['recovery']
            assert root['state'] == 'failed' and episode['status'] == 'blocked' and episode['attempts'] == 3
            assert episode['failure_scope']['provider'] == 'direct_http'
            assert episode['failure_scope']['origin'] == 'https://original.recovery-fixture.test'
            assert alternative['result']['output']['outcome'] == 'completed'
            evidence = alternative['result']['output']['evidence_refs']
            assert len(evidence) == 1 and evidence[0]['kind'] == 'document'
            with tx(aid) as connection:
                document = one(connection, 'SELECT * FROM lead.knowledge_documents WHERE id=%s', (evidence[0]['id'],))
            assert document['source_ref'] == ALTERNATIVE and QUOTE in Path(document['body_ref']).read_text()
            assert len([call for call in transport_calls if call[0] == ORIGINAL]) == 3
            assert not any(call[0].endswith(('/one', '/two')) for call in transport_calls)

            # Ask the real bounded scheduler to inspect its own recorded due time.
            runtime.recover_due(aid, now=datetime.fromisoformat(episode['next_check_at']))
            await pump_until(lambda rows: any(row['type'] == 'growth.recover' and row['state'] == 'succeeded' for row in rows))
            rows = work_rows(aid)
            blocked_probe = next(row for row in rows if row['type'] == 'growth.recover')
            assert blocked_probe['result']['output']['output']['state'] == 'still_blocked'
            status = jsonable_encoder(views.render_growth(aid))
            assert {root_id, str(purpose(rows, 'waiting-one')['id']), str(purpose(rows, 'waiting-two')['id'])} <= {item['id'] for item in status['waiting']}
            recent = next(item for item in status['recent_work'] if item['id'] == str(blocked_probe['id']))
            assert recent['state'] == 'succeeded' and recent['probe']['state'] == 'still_blocked'
            assert purpose(rows, 'blocked-original')['result']['runtime']['recovery']['status'] == 'blocked'

            if expire_second:
                # Let the ordinary workflow's recorded task window expire. No
                # state, success, claim or proof is inserted by the test.
                await pump_until(lambda rows: purpose(rows, 'waiting-two')['state'] == 'failed')
                assert purpose(work_rows(aid), 'waiting-two')['result']['runtime']['reason'] == 'work_window_ended'
            current_names = ('blocked-original', 'waiting-one') if expire_second else ('blocked-original', 'waiting-one', 'waiting-two')

            # Two real scheduled probes race for the original lease. Only the
            # winner may obtain a source proof; the loser must not clear scope.
            source_ready = True
            for _ in range(2):
                episode = purpose(work_rows(aid), 'blocked-original')['result']['runtime']['recovery']
                runtime.recover_due(aid, now=datetime.fromisoformat(episode['next_check_at']))
            await pump_until(lambda rows: probe_entered.is_set() and any(row['type'] == 'growth.recover'
                and row['result'].get('output', {}).get('output', {}).get('state') == 'another_probe_active' for row in rows))
            assert purpose(work_rows(aid), 'blocked-original')['result']['runtime']['recovery']['status'] == 'blocked'
            release_probe.set()
            await pump_until(lambda rows: all(purpose(rows, name)['state'] == 'succeeded'
                for name in current_names) and all(row['state'] == 'succeeded'
                for row in rows if row['type'] == 'growth.decision') and any(
                    any(trigger.get('reason') == 'resource_recovered' for trigger in decision['triggers']) for decision in decisions))
            rows = work_rows(aid)
            recovered = next(row for row in rows if row['type'] == 'growth.recover'
                             and row['result']['output'].get('output', {}).get('state') == 'recovered')
            proof = recovered['result']['output']['output']
            assert proof['resumed_count'] == len(current_names)
            assert {ref['task_id'] for ref in proof['resumed_refs']} == {
                str(purpose(rows, name)['id']) for name in current_names}
            for name in current_names:
                row = purpose(rows, name)
                assert row['input']['task_generation'] == 2
                assert not row['result']['runtime']['blocked_resources']
                assert row['result']['output']['outcome'] == 'completed' and row['result']['output']['evidence_refs']
            if expire_second:
                expired = purpose(rows, 'waiting-two')
                assert expired['state'] == 'failed' and expired['input']['task_generation'] == 1
                assert str(expired['id']) not in {item['id'] for item in jsonable_encoder(views.render_growth(aid))['current_work']}
            assert purpose(rows, 'feasible-alternative')['input']['task_generation'] == 1
            with tx(aid) as connection:
                original_document = one(connection, 'SELECT source_ref FROM lead.knowledge_documents WHERE id=%s', (proof['proof_ref'],))
            assert original_document['source_ref'] == ORIGINAL
            assert len([call for call in transport_calls if call[1] and call[2]]) == 1

            # At-least-once outbox delivery reuses the exact completed probe
            # Workflow ID, rather than invoking another proof or business wake.
            ref = {'agent_id': aid, 'task_id': str(recovered['id']), 'task_generation': recovered['input']['task_generation']}
            with tx(aid) as connection:
                emit(connection, aid, 'growth.work', ref)
                emit(connection, aid, 'growth.work', ref)
            def delivered(_rows):
                with tx(aid) as connection:
                    pending = one(connection, 'SELECT count(*) n FROM lead.outbox_events WHERE published_at IS NULL')['n']
                    triggers = one(connection, "SELECT onboarding_state#>'{growth_runtime,pending_triggers}' pending FROM lead.agents WHERE id=%s", (aid,))['pending']
                    return pending == 0 and not triggers
            await pump_until(delivered)
            with tx(aid) as connection:
                wakes = all_rows(connection, "SELECT * FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='resource_recovered'")
                state = one(connection, "SELECT onboarding_state->'growth_runtime' runtime FROM lead.agents WHERE id=%s", (aid,))['runtime']
            assert len(wakes) == 1 and wakes[0]['published_at'] is not None
            assert len([decision for decision in decisions if any(t.get('reason') == 'resource_recovered' for t in decision['triggers'])]) == 1
            assert not state.get('pending_triggers')
            assert len([call for call in transport_calls if call[1] and call[2]]) == 1
            for row in (root, recovered, purpose(rows, 'waiting-one')):
                generation = row['input']['task_generation']
                handle = client.get_workflow_handle(f"growth-work:{row['id']}:{generation}")
                await Replayer(workflows=[GrowthWorkWorkflow]).replay_workflow(await handle.fetch_history())
    finally:
        release_probe.set()
        for identity in routed.ids:
            try:
                await client.get_workflow_handle(identity).terminate('Disposable integration case finished')
            except Exception as error:
                # Completed work is already closed; only a missing/closed run is benign.
                from temporalio.service import RPCError, RPCStatusCode
                if not isinstance(error, RPCError) or error.status not in (RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION):
                    raise
