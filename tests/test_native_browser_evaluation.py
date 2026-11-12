"""One A-only full dry orchestration; no real provider or unseen B source."""

import asyncio
import json
import os
import socket

import pytest


def test_default_prereg_is_a_draft_with_exact_bounded_stages(tmp_path):
    from scripts.evaluate_native_browser import FRAGMENT, STAGES, plan, private_write
    draft = plan()
    assert draft['state'] == 'draft' and draft['stages'] == list(STAGES)
    assert FRAGMENT == {'requests': 8, 'tokens_and_reserved_units': 40000, 'active_steps': 20, 'seconds': 180, 'output_tokens': 1800}
    assert draft['total_new_ceiling'] == {'requests': 97, 'tokens_and_reserved_units': 520000}
    path = tmp_path/'draft.json'
    private_write(path, draft)
    assert path.stat().st_mode & 0o077 == 0
    assert draft['private_config_sha256'] is None and draft['model'] is None


@pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1', reason='Explicit isolated Linux product run required')
@pytest.mark.asyncio
async def test_first_failed_result_stops_before_a_later_task(monkeypatch):
    """Orchestration-only negative: no SDK/model is started for this failure."""
    from types import SimpleNamespace

    from lead_app.config import STATE
    from lead_app.growth import runtime

    from scripts.evaluate_native_browser import AcceptanceStop, NativeCase

    class Case(NativeCase):
        def ledger(self):
            return [], []

        def row(self, _):
            return {'state': 'failed'}

    entered = []

    async def before(name, _):
        entered.append(name)

    async def failed(_):
        return {'outcome': 'failed', 'failure_code': 'synthetic_failure'}

    monkeypatch.setattr(runtime, 'advance', lambda *args, **kwargs: {'state': 'ready', 'claim': {}})
    monkeypatch.setattr(runtime, 'execute', failed)
    monkeypatch.setattr(runtime, 'finish', lambda *args: None)
    site = SimpleNamespace(case_id='failure', snapshot=lambda: {'requests': []})
    case = Case('a', site, 'http://127.0.0.1', before_stage=before, output=STATE/'c22-first-failure.json')
    ref = {'task_id': 'original-task', 'task_generation': 1}
    with pytest.raises(AcceptanceStop, match='bounded_stage_failed:login'):
        for name in ('login', 'discover_publish', 'publish'):
            await case.perform(name, ref)
    assert entered == ['login'] and [row['stage'] for row in case.report['stages']] == ['login']
    assert case.report['stages'][0]['failure_code'] == 'synthetic_failure'


@pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1', reason='Explicit isolated Linux product run required')
@pytest.mark.asyncio
async def test_a_six_tasks_and_zero_model_unknown_seed(monkeypatch):
    import uvicorn
    from lead_app import browser, domain, native_runner
    from lead_app.config import STATE
    from lead_app.db import tx
    from lead_app.forward_proxy import BrowserForwardProxy
    from native_browser_evaluation import create_a, dry_model

    from scripts.evaluate_native_browser import NativeCase

    site = create_a()
    calls = []
    model_server, state, model = dry_model(site, calls, STATE/'c22-dry-model.json')
    monkeypatch.setattr(native_runner, 'resolve_model', lambda _: model)

    async def connect(self, ip, port):
        if ip != '93.184.215.14' or port != 80:
            raise ValueError('a_dry_destination_refused')
        return await asyncio.open_connection('127.0.0.1', site.listener_port)

    monkeypatch.setattr(BrowserForwardProxy, 'connect', connect)
    with socket.socket() as chooser:
        chooser.bind(('127.0.0.1', 0))
        port = chooser.getsockname()[1]
    endpoint = 'http://127.0.0.1:'+str(port)
    monkeypatch.setenv('LEAD_BROWSER_TRANSPORTS', json.dumps({'shared': endpoint}))
    server = uvicorn.Server(uvicorn.Config(browser.app, host='127.0.0.1', port=port, log_level='error'))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(.05)
    aid = None
    evidence = {'publish': 'Frozen A first publication for the six-task acceptance.',
                'reply': 'Frozen A reply to that original publication.',
                'unknown': 'Frozen A separately dispatched unknown receipt acceptance.'}

    async def before(name, case):
        state.update(stage=name, step=0)
        state['editor_url'] = site.target_url
        if name == 'discover_reply':
            state['editor_url'] = case.report['stages'][2]['receipt']['url']
        state['body'] = evidence.get(name) or evidence['unknown']

    try:
        aid = domain.create_agent(runtime_engine='growth')
        with tx(aid) as c:
            c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        case = NativeCase(aid, site, endpoint, before_stage=before, output=STATE/'c22-dry-a.json')
        report = await case.run(evidence)
        assert report['completed'] and report['first_failure'] is None
        assert [row['requests'] for row in report['stages']] == [5, 2, 3, 2, 3, 2]
        assert len(calls) == 17 and not any(row['username_literal_seen'] or row['password_literal_seen'] for row in calls)
        assert report['mechanical_seed']['requests'] == report['mechanical_seed']['reservation_units'] == 0
        assert len(site.snapshot()['posts']) == 3
        assert all(report['stages'][index]['receipt']['independent_dom_receipt'] for index in (2, 4, 5))
        assert report['stages'][5]['receipt']['action_id'] == report['mechanical_seed']['action_id']
        print('C22_DRY', {'stage_requests': [row['requests'] for row in report['stages']],
                         'seed_model_requests': 0, 'source_posts': 3,
                         'id_attribute_visible': any(row['id_attribute_visible'] for row in calls),
                         'name_attribute_visible': any(row['name_attribute_visible'] for row in calls),
                         'rel_attribute_visible': any(row['rel_attribute_visible'] for row in calls)})
    finally:
        if aid:
            await browser.gateway.close(aid)
            domain.set_control(aid, 'archive')
        server.should_exit = True
        await serving
        site.close()
        model_server.shutdown()
        model_server.server_close()
