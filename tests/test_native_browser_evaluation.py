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
    assert FRAGMENT == {'requests': 8, 'tokens_and_reserved_units': 200000, 'active_steps': 20, 'seconds': 180, 'output_tokens': 8000}
    assert draft['total_new_ceiling'] == {'requests': 97, 'tokens_and_reserved_units': 2440000}
    path = tmp_path/'draft.json'
    private_write(path, draft)
    assert path.stat().st_mode & 0o077 == 0
    assert draft['private_config_sha256'] is None and draft['model'] is None


def test_prereg_freezes_namespace_dependencies_and_transitive_site_sources():
    from scripts.evaluate_native_browser import plan
    draft = plan()
    assert {'scripts/browser_namespace.py', 'requirements.lock.txt', 'requirements.browser-use.lock.txt',
            'tests/native_browser_fixture.py', 'tests/native_browser_fixture_b.py'} <= set(draft['source_hashes'])
    assert draft['provider_credential_sha256'] is None


def test_prereg_validation_in_fresh_no_site_python(tmp_path):
    """The real -S entry must not depend on imports already loaded by pytest."""
    import subprocess
    import sys
    from pathlib import Path

    from scripts.evaluate_native_browser import plan, private_write, sha

    adapter = tmp_path/'standalone_adapter.py'
    adapter.write_text("raise AssertionError('Validation must not execute the adapter')\n")
    config = {'model': {'provider': 'fixture', 'model': 'fixture', 'base_url': 'http://127.0.0.1'},
        'sites': [{'adapter': 'standalone_adapter:create'}]}
    private = tmp_path/'registered.json'
    private_write(private, config)
    prereg = plan()
    prereg.update(state='frozen', model=config['model'], private_config_sha256=sha(private.read_bytes()),
        site_adapter_sha256={'standalone_adapter:create': sha(adapter.read_bytes())})
    registered = tmp_path/'prereg.json'
    private_write(registered, prereg)
    source = Path(__file__).resolve().parents[1]/'scripts/evaluate_native_browser.py'
    program = ("import pathlib,sys\nsource=pathlib.Path(sys.argv[1])\n"
        "namespace={'__file__':str(source),'__name__':'isolated_prereg'}\n"
        "exec(compile(source.read_text(),str(source),'exec'),namespace)\n"
        "namespace['validate_prereg'](sys.argv[2],sys.argv[3],sys.argv[4])\n")
    result = subprocess.run([sys.executable, '-S', '-c', program, str(source), str(registered),
        str(private), sha(registered.read_bytes())], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_aggregate_allowance_counts_completed_stages_across_both_sites():
    from scripts import evaluate_native_browser as evaluation
    usage = [{'units': 1200, 'details': {'requests': 6}},
             {'units': 1800, 'details': {'requests': 5}}]
    assert evaluation.require_run_allowance({'requests': 18, 'tokens_and_reserved_units': 480000}, [], usage) == 7
    assert evaluation.require_run_allowance({'requests': 19, 'tokens_and_reserved_units': 203000}, [], usage) == 8
    with pytest.raises(evaluation.AcceptanceStop, match='aggregate_request_allowance'):
        evaluation.require_run_allowance({'requests': 11, 'tokens_and_reserved_units': 480000}, [], usage)


@pytest.mark.parametrize('reservations,usage,limit,reason', [
    ([], [{'units': 1, 'details': {'requests': 0}}], 40000, 'aggregate_token_allowance'),
    ([{'state': 'unknown'}], [], 480000, 'unresolved_prior_reservation'),
    ([{'state': 'reserved'}], [], 480000, 'unresolved_prior_reservation'),
    ([], [{'units': 0, 'details': {}}], 480000, 'unmeasured_prior_requests'),
    ([], [{'units': 0, 'details': {'requests': True}}], 480000, 'unmeasured_prior_requests'),
    ([], [{'units': 0, 'details': ['private-value']}], 480000, 'unmeasured_prior_requests'),
    ([], [{'units': '20', 'details': {'requests': 1}}], 480000, 'unmeasured_prior_token_units'),
])
def test_aggregate_allowance_stops_unmeasured_or_insufficient_budget(reservations, usage, limit, reason):
    from scripts import evaluate_native_browser as evaluation
    with pytest.raises(evaluation.AcceptanceStop, match=reason):
        evaluation.require_run_allowance({'requests': 96, 'tokens_and_reserved_units': limit}, reservations, usage)


@pytest.mark.parametrize('allowance', [
    {'requests': 97, 'tokens_and_reserved_units': 480000},
    {'requests': 96, 'tokens_and_reserved_units': 2400001},
    {'requests': True, 'tokens_and_reserved_units': 480000},
])
def test_aggregate_allowance_cannot_expand_the_registered_native_ceiling(allowance):
    from scripts.evaluate_native_browser import validate_run_allowance
    with pytest.raises(ValueError, match='invalid_aggregate_native_allowance'):
        validate_run_allowance(allowance)


@pytest.mark.parametrize('requests,units,reservations,reason', [
    (1, 40000, [], None),
    (2, 40000, [], 'aggregate_allowance_exceeded'),
    (1, 40001, [], 'aggregate_allowance_exceeded'),
    (1, 40000, [{'state': 'unknown'}], 'unresolved_prior_reservation'),
])
def test_final_stage_checks_actual_totals_without_reserving_another_fragment(requests, units, reservations, reason):
    from scripts.evaluate_native_browser import AcceptanceStop, verify_run_spend
    allowance = {'requests': 1, 'tokens_and_reserved_units': 40000}
    usage = [{'units': units, 'details': {'requests': requests}}]
    if reason:
        with pytest.raises(AcceptanceStop, match=reason):
            verify_run_spend(allowance, reservations, usage)
    else:
        verify_run_spend(allowance, reservations, usage)


@pytest.mark.asyncio
async def test_aggregate_allowance_stops_before_task_admission(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from lead_app.growth import runtime

    from scripts.evaluate_native_browser import AcceptanceStop, NativeCase

    class Case(NativeCase):
        def ledger(self):
            return [], [{'units': 20, 'details': {'requests': 1}}]

    def forbidden(*args, **kwargs):
        raise AssertionError('No task admission or model call is allowed after the aggregate limit')

    monkeypatch.setattr(runtime, 'advance', forbidden)
    case = Case('synthetic', SimpleNamespace(case_id='budget'), 'http://unused',
        output=tmp_path/'case.json', run_allowance={'requests': 1, 'tokens_and_reserved_units': 480000})
    with pytest.raises(AcceptanceStop, match='aggregate_request_allowance'):
        await case.perform('login', {'task_id': 'original', 'task_generation': 1})


@pytest.mark.asyncio
async def test_changed_provider_credential_stops_before_any_task_or_browser(monkeypatch,tmp_path):
    from lead_app import config, domain

    from scripts.evaluate_native_browser import execute_registered, sha

    runtime=tmp_path/'services/browser-runtime'
    runtime.mkdir(parents=True)
    (runtime/'manifest.json').write_text('{"synthetic":true}')
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setattr(config,'env',lambda:{'LEAD_DB_HOST':'127.0.0.1','LEAD_DB_NAME':'native_eval_fixture',
        'LEAD_NETWORK_NAMESPACE':'1'})
    metadata={'provider':'deepseek','model':'synthetic-only','base_url':'https://api.deepseek.com'}
    monkeypatch.setattr(config,'model_config',lambda:{**metadata,'key':'different-synthetic-credential'})
    def forbidden(*args,**kwargs):
        raise AssertionError('No task or browser may start with a changed credential')
    monkeypatch.setattr(domain,'create_agent',forbidden)
    prereg={'runtime_manifest_sha256':sha((runtime/'manifest.json').read_bytes()),
        'provider_credential_sha256':sha(b'original-synthetic-credential')}
    with pytest.raises(ValueError,match='registered_provider_credential_mismatch'):
        await execute_registered(prereg,{'model':metadata,'sites':[]},tmp_path/'result.json')


@pytest.mark.parametrize('images,require_visual,accepted', [
    (None, True, False), (0, True, False), (True, True, False),
    (-1, True, False), (2, True, False), (1, True, True),
    (None, False, True), (0, False, True),
])
@pytest.mark.asyncio
async def test_final_visual_trial_requires_actual_images_before_another_stage(
        monkeypatch, tmp_path, images, require_visual, accepted):
    """A successful text-only task cannot stand in for the visual experiment."""
    from types import SimpleNamespace

    from lead_app import config
    from lead_app.growth import runtime

    from scripts.evaluate_native_browser import AcceptanceStop, NativeCase

    monkeypatch.setattr(config, 'STATE', tmp_path)
    completed = []

    class Case(NativeCase):
        def ledger(self):
            if not completed:
                return [], []
            return ([{'id': 'reservation', 'state': 'settled', 'units': 40000}],
                [{'reservation_id': 'reservation', 'units': 20, 'details': {'requests': 1}}])

        def row(self, _):
            return {'state': 'succeeded'}

    async def execute(_):
        folder = tmp_path/'browser-use-runs'/'visual-worker'
        folder.mkdir(parents=True)
        usage = {'request_count': 1, 'entry_count': 1, 'total_tokens': 20,
            'billing_unknown': False, 'unmeasured_requests': 0}
        if images is not None:
            usage['vision_images_sent'] = images
        (folder/'result.json').write_text(json.dumps({'usage': usage}))
        completed.append('discover_publish')
        return {'outcome': 'completed'}

    monkeypatch.setattr(runtime, 'advance', lambda *args, **kwargs: {'state': 'ready', 'claim': {}})
    monkeypatch.setattr(runtime, 'execute', execute)
    monkeypatch.setattr(runtime, 'finish', lambda *args: None)
    site = SimpleNamespace(case_id='visual', snapshot=lambda: {'requests': []})
    case = Case('synthetic', site, 'http://unused', output=tmp_path/'case.json',
        require_visual=require_visual)
    ref = {'task_id': 'original-task', 'task_generation': 1}
    if accepted:
        await case.perform('discover_publish', ref)
    else:
        with pytest.raises(AcceptanceStop, match='visual_image_missing_or_invalid:discover_publish'):
            for name in ('discover_publish', 'publish'):
                await case.perform(name, ref)
    assert completed == ['discover_publish']
    assert [row['stage'] for row in case.report['stages']] == ['discover_publish']


@pytest.mark.asyncio
async def test_registered_two_domains_reach_only_their_own_source_through_original_proxy(monkeypatch):
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from uuid import uuid4

    from lead_app import forward_proxy

    from scripts.evaluate_native_browser import synthetic_site_transport

    received=[]
    servers=[]
    async def source(number,reader,writer):
        try:
            header=await reader.readuntil(b'\r\n\r\n')
            received.append((number,header))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
            await writer.drain()
        finally:
            writer.close()
    for number in range(2):
        servers.append(await asyncio.start_server(lambda reader,writer,n=number:source(n,reader,writer),'127.0.0.1',0))
    sites=[SimpleNamespace(origin='http://'+host,listener_port=server.sockets[0].getsockname()[1])
        for host,server in zip(('alpha-c22.test','beta-c22.test'),servers,strict=True)]
    original=forward_proxy.BrowserForwardProxy.resolve,forward_proxy.BrowserForwardProxy.connect
    try:
        with tempfile.TemporaryDirectory(prefix='c22-net-',dir='/private/tmp' if os.name!='nt' and os.path.isdir('/private/tmp') else None) as folder:
            monkeypatch.setattr(forward_proxy,'STATE',Path(folder))
            with synthetic_site_transport(sites):
                proxy=await forward_proxy.BrowserForwardProxy(str(uuid4()),forward_guard=lambda:'synthetic-only').start()
                async def get(host):
                    reader,writer=await asyncio.open_connection('127.0.0.1',proxy.port)
                    try:
                        writer.write((f'GET http://{host}/probe HTTP/1.1\r\nHost: {host}\r\n'
                            f'Proxy-Authorization: {proxy.auth}\r\nConnection: close\r\n\r\n').encode())
                        await writer.drain()
                        return await asyncio.wait_for(reader.read(),2)
                    finally:
                        writer.close()
                        await writer.wait_closed()
                try:
                    for host in ('alpha-c22.test','beta-c22.test'):
                        assert b'200 OK' in await get(host)
                    for host in ('outside-c22.test','alpha-c22.test:81','127.0.0.1'):
                        assert b'200 OK' not in await get(host)
                finally:
                    await proxy.close()
        assert len(received)==2
        assert received[0][0]==0 and b'Host: alpha-c22.test' in received[0][1]
        assert received[1][0]==1 and b'Host: beta-c22.test' in received[1][1]
        assert all(b'proxy-authorization' not in value.lower() for _,value in received)
        assert (forward_proxy.BrowserForwardProxy.resolve,forward_proxy.BrowserForwardProxy.connect)==original
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


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
    from native_browser_evaluation import create_a, dry_model

    from scripts.evaluate_native_browser import NativeCase, synthetic_site_transport

    site = create_a('alpha-c22.test')
    calls = []
    model_server, state, model = dry_model(site, calls, STATE/'c22-dry-model.json')
    monkeypatch.setattr(native_runner, 'resolve_model', lambda _: model)

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
        with synthetic_site_transport([site]):
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
