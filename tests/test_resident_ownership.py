"""Resident routing uses operator configuration, without touching live state or queues."""

import asyncio
import importlib.util
from pathlib import Path

import pytest

OWNER = '00000000-0000-0000-0000-000000000001'
OTHER = '00000000-0000-0000-0000-000000000002'


@pytest.fixture(autouse=True)
def synthetic_routing_scope(monkeypatch):
    # These routing unit tests intentionally use two non-existent project IDs.
    # The production wrapper now establishes scope before consulting ownership.
    from lead_app import identity_store
    from lead_app.config import TENANT
    original=identity_store.agent_tenant
    monkeypatch.setattr(identity_store,'agent_tenant',lambda aid:TENANT if aid in (OWNER,OTHER) else original(aid))


def test_resident_ownership_defaults_to_no_exclusions():
    from lead_app.resident_ownership import resident_cycle_agent_ids

    assert resident_cycle_agent_ids({}) == frozenset()
    assert resident_cycle_agent_ids({'LEAD_RESIDENT_CYCLE_AGENT_IDS': ''}) == frozenset()
    assert resident_cycle_agent_ids({'LEAD_RESIDENT_CYCLE_AGENT_IDS': f' {OWNER}, {OTHER} '}) == {OWNER, OTHER}


@pytest.mark.parametrize('value', ['invalid', OWNER + ',', ',' + OWNER, OWNER + ',,' + OTHER,
                                  OWNER + ',' + OWNER, OWNER.replace('-', ''), 1])
def test_resident_ownership_rejects_invalid_operator_config(value):
    from lead_app.resident_ownership import resident_cycle_agent_ids

    with pytest.raises(ValueError, match='LEAD_RESIDENT_CYCLE_AGENT_IDS'):
        resident_cycle_agent_ids({'LEAD_RESIDENT_CYCLE_AGENT_IDS': value})


@pytest.mark.asyncio
async def test_owned_cycle_skips_original_decorators_and_other_agents_keep_original(monkeypatch):
    from lead_app import worker

    calls = []
    async def original(args):
        calls.append(args)
        return {'original': args}
    monkeypatch.setattr(worker, 'managed_autonomous_cycle', original)
    monkeypatch.setattr(worker, 'env', lambda: {'LEAD_RESIDENT_CYCLE_AGENT_IDS': OWNER})
    owned = {'agent_id': OWNER, 'cycle_id': 'fixture-cycle'}
    assert await worker.autonomous_cycle(owned) == {
        'delay': 300, 'cadence_version': 1, 'reason': 'device_resident_owns_cycle',
    }
    assert calls == []
    other = {'agent_id': OTHER, 'cycle_id': 'fixture-other'}
    assert await worker.autonomous_cycle(other) == {'original': other}
    assert calls == [other]
    assert calls[0] is other
    # The registered Temporal name is the durable history contract.
    from temporalio import activity
    assert activity._Definition.must_from_callable(worker.autonomous_cycle).name == 'autonomous_cycle'


@pytest.mark.asyncio
async def test_resident_routing_is_dynamic_and_default_preserves_original(monkeypatch):
    from lead_app import worker

    settings = {}
    calls = []
    async def original(args):
        calls.append(args)
        return {'ordinary': True}
    monkeypatch.setattr(worker, 'managed_autonomous_cycle', original)
    monkeypatch.setattr(worker, 'env', lambda: settings)
    args = {'agent_id': OWNER, 'cycle_id': 'fixture-dynamic-cycle'}
    assert await worker.autonomous_cycle(args) == {'ordinary': True}
    settings['LEAD_RESIDENT_CYCLE_AGENT_IDS'] = OWNER
    assert (await worker.autonomous_cycle(args))['reason'] == 'device_resident_owns_cycle'
    settings['LEAD_RESIDENT_CYCLE_AGENT_IDS'] = OTHER
    assert await worker.autonomous_cycle(args) == {'ordinary': True}
    assert calls == [args, args]
    assert all(call is args for call in calls)


@pytest.mark.asyncio
async def test_failed_or_blocked_cycle_does_not_hold_other_agents(monkeypatch):
    from lead_app import worker

    blocked = asyncio.Event()
    entered = asyncio.Event()
    calls = []
    async def original(args):
        calls.append(args)
        if args.get('fail'):
            raise RuntimeError('fixture failure')
        if args.get('block'):
            entered.set()
            await blocked.wait()
        return {'agent_id': args['agent_id']}
    monkeypatch.setattr(worker, 'managed_autonomous_cycle', original)
    monkeypatch.setattr(worker, 'env', lambda: {})
    failed = {'agent_id': OWNER, 'cycle_id': 'fixture-failed-cycle', 'fail': True}
    waiting = {'agent_id': OWNER, 'cycle_id': 'fixture-blocked-cycle', 'block': True}
    other = {'agent_id': OTHER, 'cycle_id': 'fixture-independent-cycle'}
    with pytest.raises(RuntimeError, match='fixture failure'):
        await worker.autonomous_cycle(failed)
    first = asyncio.create_task(worker.autonomous_cycle(waiting))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await asyncio.wait_for(worker.autonomous_cycle(other), 1) == {'agent_id': OTHER}
        assert not first.done()
    finally:
        blocked.set()
        assert await first == {'agent_id': OWNER}
    assert calls == [failed, waiting, other]
    assert all(actual is expected for actual, expected in zip(calls, (failed, waiting, other), strict=True))


@pytest.mark.asyncio
async def test_worker_rejects_invalid_ownership_before_connecting(monkeypatch):
    from lead_app import worker

    async def connect():
        pytest.fail('invalid ownership must fail before connecting or polling any queue')
    monkeypatch.setattr(worker, 'connect', connect)
    monkeypatch.setattr(worker, 'env', lambda: {'LEAD_RESIDENT_CYCLE_AGENT_IDS': 'invalid'})
    with pytest.raises(ValueError, match='LEAD_RESIDENT_CYCLE_AGENT_IDS'):
        await worker.main()


@pytest.mark.asyncio
async def test_reddit_adapter_uses_unchanged_worker_entrypoint(monkeypatch):
    from lead_app import worker

    calls = []
    async def main():
        calls.append(True)
    monkeypatch.setattr(worker, 'main', main)
    path = Path(__file__).resolve().parents[1] / 'services/channel_adapters/reddit/entry.py'
    spec = importlib.util.spec_from_file_location('reddit_adapter_fixture', path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    await adapter.run(object())
    assert calls == [True]
    assert adapter.worker is worker


@pytest.mark.asyncio
@pytest.mark.parametrize('lane,queue', [('research', 'lead-research'), ('delivery', 'lead-delivery')])
async def test_resident_ownership_does_not_exclude_channel_clock(monkeypatch, lane, queue):
    from contextlib import nullcontext

    from lead_app import worker

    event = {'id': 'fixture-event', 'agent_id': OWNER, 'event_type': 'channel.clock',
             'occurred_at': 0, 'payload': {'task_id': 'fixture-task', 'generation': 1, 'lane': lane}}
    launches, acknowledgements = [], []
    async def run_db(fn, *args):
        return fn(*args)
    async def launch(client, *args, **kwargs):
        launches.append((args, kwargs))
    monkeypatch.setattr(worker, 'env', lambda: {'LEAD_RESIDENT_CYCLE_AGENT_IDS': OWNER})
    monkeypatch.setattr(worker, 'tenant_scope', lambda *a: nullcontext())
    monkeypatch.setattr(worker, 'service_scope', nullcontext)
    monkeypatch.setattr(worker, 'run_db', run_db)
    monkeypatch.setattr(worker, 'outbox_rows', lambda: [event])
    monkeypatch.setattr(worker, 'outbox_agent', lambda aid: {'desired_state': 'running'})
    monkeypatch.setattr(worker, 'ack_outbox', lambda *a: acknowledgements.append(a))
    monkeypatch.setattr(worker, 'defer_outbox', lambda *a: pytest.fail('channel delivery must not be deferred'))
    monkeypatch.setattr(worker, 'start_workflow', launch)
    await worker.deliver_workspace(object(), 'fixture-tenant', True, True)
    assert len(launches) == 1
    assert launches[0][1]['task_queue'] == queue
    assert launches[0][0][1]['agent_id'] == OWNER
    assert acknowledgements == [(OWNER, 'fixture-event', None)]
