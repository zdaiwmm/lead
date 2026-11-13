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


def test_prereg_freezes_namespace_dependencies_and_transitive_site_sources():
    from scripts.evaluate_native_browser import plan
    draft = plan()
    assert {'scripts/browser_namespace.py', 'requirements.lock.txt', 'requirements.browser-use.lock.txt',
            'tests/native_browser_fixture.py', 'tests/native_browser_fixture_b.py'} <= set(draft['source_hashes'])
    assert draft['provider_credential_sha256'] is None


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
