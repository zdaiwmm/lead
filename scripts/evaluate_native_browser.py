"""Preregister, then explicitly run six bounded native-browser acceptance tasks.

The default command only writes a plan. Execution requires an exact plan hash,
private configuration hash and production source manifest. No fixture recipes,
model responses or credentials are embedded in the paid executor.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import stat
import time
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('login', 'discover_publish', 'publish', 'discover_reply', 'reply', 'reconcile')
FRAGMENT = {'requests': 8, 'tokens_and_reserved_units': 40000, 'active_steps': 20,
            'seconds': 180, 'output_tokens': 1800}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def source_manifest(root=ROOT):
    files = [*root.joinpath('services/api/lead_app').rglob('*.py'),
             *root.joinpath('infra/migrations').rglob('*.py'),
             *root.joinpath('infra/migrations/sql').glob('*.sql'),
             *root.joinpath('tests').glob('native_browser_fixture*.py'),
             root/'requirements.lock.txt', root/'requirements.browser-use.lock.txt',
             root/'scripts/browser_namespace.py',
             root/'scripts/browser_use_runner.py', root/'scripts/evaluate_native_browser.py',
             root/'scripts/native_eval_seed.py']
    return {str(path.relative_to(root)): sha(path.read_bytes()) for path in sorted(set(files))}


def plan(root=ROOT):
    return {'version': 'native-c22-six-tasks-v1', 'state': 'draft',
            'source_hashes': source_manifest(root), 'private_config_sha256': None,
            'runtime_manifest_sha256': None, 'site_adapter_sha256': {}, 'model': None,
            'provider_credential_sha256': None,
            'stages': list(STAGES), 'fragment': FRAGMENT,
            'per_site': {'fragments': 6, 'requests': 48, 'tokens_and_reserved_units': 240000},
            'two_sites': {'requests': 96, 'tokens_and_reserved_units': 480000},
            'optional_business': {'requests': 1, 'tokens_and_reserved_units': 40000,
                                  'repair': False, 'business_execution': False},
            'execution_allowance': None,
            'total_new_ceiling': {'requests': 97, 'tokens_and_reserved_units': 520000},
            'prior_unknown_units': 80000,
            'stop': 'First failed/waiting/unknown paid stage or unmeasured request stops all later stages and sites; no retries or paid continuation.',
            'seed': 'Separate zero-model SDK input/click of the server-learned publish method; lose its acknowledgement once. Never manufacture unknown in SQL.',
            'site_protocol': ['case_id', 'origin', 'entry_url', 'target_url', 'username', 'password',
                              'expected_subject', 'listener_port', 'snapshot()', 'arm_drop_ack()', 'close()'],
            'mechanical_minimum_requests': [5, 2, 3, 2, 3, 2],
            'coverage': 'Two synthetic sites only. No real accounts or production operations. Mechanical results do not prove model capability.'}


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
    path.chmod(0o600)


def validate_prereg(prereg, private_path, expected_hash):
    raw = Path(prereg).read_bytes()
    if sha(raw) != expected_hash:
        raise ValueError('prereg_hash_mismatch')
    data = json.loads(raw)
    private_path = Path(private_path)
    if private_path.is_symlink() or stat.S_IMODE(private_path.stat().st_mode) & 0o077:
        raise ValueError('private_config_must_be_owner_only_regular_file')
    if data['state'] != 'frozen' or data['source_hashes'] != source_manifest():
        raise ValueError('producer_source_not_frozen_exactly')
    if data['private_config_sha256'] != sha(private_path.read_bytes()):
        raise ValueError('private_config_hash_mismatch')
    if data['stages'] != list(STAGES) or data['fragment'] != FRAGMENT:
        raise ValueError('fragment_contract_changed')
    validate_run_allowance(data.get('execution_allowance'))
    config = json.loads(private_path.read_text())
    if not 1 <= len(config['sites']) <= 2:
        raise ValueError('exactly_one_or_two_registered_sites_required')
    if set(config) != {'sites', 'model'} or config['model'] != data['model']:
        raise ValueError('registered_model_mismatch')
    for spec in config['sites']:
        module, _ = spec['adapter'].split(':', 1)
        location = importlib.util.find_spec(module).origin
        if data['site_adapter_sha256'].get(spec['adapter']) != sha(Path(location).read_bytes()):
            raise ValueError('site_adapter_hash_mismatch')
    return data, config


class AcceptanceStop(ValueError):
    """A preserved first failure; the caller must not continue to another site."""


def validate_run_allowance(allowance):
    if allowance is None:
        return
    if (not isinstance(allowance, dict) or set(allowance) != {'requests', 'tokens_and_reserved_units'}
            or type(allowance['requests']) is not int or not 0 <= allowance['requests'] <= 96
            or type(allowance['tokens_and_reserved_units']) is not int
            or not 0 <= allowance['tokens_and_reserved_units'] <= 480000):
        raise ValueError('invalid_aggregate_native_allowance')


def measured_run_totals(reservations, usage):
    if any(row.get('state') not in ('settled', 'released') for row in reservations):
        raise AcceptanceStop('unresolved_prior_reservation')
    requests = units = 0
    for row in usage:
        details = row.get('details')
        count = details.get('requests') if isinstance(details, dict) else None
        if type(count) is not int or count < 0:
            raise AcceptanceStop('unmeasured_prior_requests')
        if type(row.get('units')) is not int or row['units'] < 0:
            raise AcceptanceStop('unmeasured_prior_token_units')
        requests += count
        units += row['units']
    return requests, units


def require_run_allowance(allowance, reservations, usage):
    """Keep the full token reserve; clamp the last fragment to remaining calls."""
    validate_run_allowance(allowance)
    if allowance is None:
        return
    requests, units = measured_run_totals(reservations, usage)
    if requests >= allowance['requests']:
        raise AcceptanceStop('aggregate_request_allowance')
    if units + FRAGMENT['tokens_and_reserved_units'] > allowance['tokens_and_reserved_units']:
        raise AcceptanceStop('aggregate_token_allowance')
    return min(FRAGMENT['requests'], allowance['requests'] - requests)


def verify_run_spend(allowance, reservations, usage):
    if allowance is None:
        return
    validate_run_allowance(allowance)
    requests, units = measured_run_totals(reservations, usage)
    if requests > allowance['requests'] or units > allowance['tokens_and_reserved_units']:
        raise AcceptanceStop('aggregate_allowance_exceeded')


@contextmanager
def stage_model_request_ceiling(aid, ceiling):
    """Only this sequential synthetic evaluator lowers its Agent's call cap."""
    if ceiling is None:
        yield FRAGMENT['requests']
        return
    if type(ceiling) is not int or not 1 <= ceiling <= FRAGMENT['requests']:
        raise ValueError('invalid_stage_request_ceiling')
    from lead_app import admin_settings

    original = admin_settings.for_agent
    settings, _ = original(aid, 'models')
    effective = min(settings['max_model_requests'], FRAGMENT['requests'], ceiling)

    def bounded(request_aid, kind):
        values, version = original(request_aid, kind)
        if str(request_aid) == str(aid) and kind == 'models':
            values = {**values, 'max_model_requests': min(values['max_model_requests'], effective)}
        return values, version

    admin_settings.for_agent = bounded
    try:
        yield effective
    finally:
        admin_settings.for_agent = original


@contextmanager
def synthetic_site_transport(sites):
    """Map registered .test hosts to their own loopback source, never the net."""
    from lead_app.forward_proxy import BrowserForwardProxy
    hosts, addresses = {}, {}
    for number, site in enumerate(sites):
        parsed = urlsplit(site.origin)
        host = parsed.hostname
        if (not host or not host.endswith('.test') or host in hosts or parsed.scheme != 'http'
                or parsed.port not in (None, 80) or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError('unique_synthetic_test_domains_required')
        address = '93.184.215.'+str(140+number)
        hosts[host] = address
        addresses[address] = site
    original = BrowserForwardProxy.resolve, BrowserForwardProxy.connect

    async def resolve(self, host):
        value = hosts.get(host.lower().rstrip('.'))
        if value is None:
            raise ValueError('synthetic_transport_host_refused')
        return str(self.public_ip(value))

    async def connect(self, ip, port):
        if ip not in addresses or port != 80:
            raise ValueError('synthetic_transport_destination_refused')
        return await asyncio.open_connection('127.0.0.1', addresses[ip].listener_port)

    BrowserForwardProxy.resolve, BrowserForwardProxy.connect = resolve, connect
    try:
        yield
    finally:
        BrowserForwardProxy.resolve, BrowserForwardProxy.connect = original


class NativeCase:
    """Generic task orchestration. The site oracle is never exposed to the SDK."""

    def __init__(self, aid, site, gateway_endpoint, *, before_stage=None, output, run_allowance=None,
                 require_visual=False):
        self.aid, self.site, self.endpoint = aid, site, gateway_endpoint
        validate_run_allowance(run_allowance)
        self.run_allowance = dict(run_allowance) if run_allowance is not None else None
        self.require_visual = require_visual
        self.before_stage = before_stage
        self.output = Path(output)
        self.report = {'case_id': site.case_id, 'stages': [], 'mechanical_seed': None,
                       'real_model': before_stage is None, 'first_failure': None}
        self.pending = self.target_id = None

    def persist(self):
        private_write(self.output, self.report)

    def row(self, ref):
        from lead_app.db import one, tx
        with tx(self.aid) as c:
            return one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))

    def ledger(self):
        from lead_app.db import all_rows, tx
        with tx(self.aid) as c:
            return all_rows(c, 'SELECT id,state,units,pricing_snapshot FROM lead.budget_reservations'), all_rows(
                c, 'SELECT reservation_id,units,details FROM lead.usage_events')

    async def perform(self, name, ref, *, expected='completed', seed=False, now=None):
        from lead_app.config import STATE
        from lead_app.growth.runtime import advance, execute, finish
        before, prior_usage = self.ledger()
        request_ceiling = None
        if not seed:
            request_ceiling = require_run_allowance(self.run_allowance, before, prior_usage)
        paths = set((STATE/'browser-use-runs').glob('*/result.json'))
        if self.before_stage:
            await self.before_stage(name, self)
        stage = {'stage': name, 'task_id': str(ref['task_id']), 'task_generation': ref['task_generation'],
                 'mechanical': seed, 'started_at_monotonic': time.monotonic()}
        source_before = sum(row['method'] == 'POST' for row in self.site.snapshot()['requests'])
        self.report['mechanical_seed' if seed else 'active_stage'] = stage
        self.persist()
        with stage_model_request_ceiling(self.aid, request_ceiling) as effective_ceiling:
            stage['request_ceiling'] = 0 if seed else effective_ceiling
            admitted = advance(ref, now=now)
            if admitted['state'] != 'ready':
                raise AcceptanceStop('task_not_admitted:' + name + ':' + admitted['state'])
            result = await execute(admitted['claim'])
            finish(admitted['claim'], result)
        current, usage = self.ledger()
        fresh = [row for row in current if row['id'] not in {row['id'] for row in before}]
        workers = []
        for path in sorted(set((STATE/'browser-use-runs').glob('*/result.json'))-paths):
            worker = json.loads(path.read_text())
            workers.append({'workdir': path.parent.name, 'usage': worker.get('usage', {}),
                            'credential_scan': worker.get('credential_scan', {})})
        stage.update(outcome=result['outcome'], failure_code=result.get('failure_code'), terminal=self.row(ref)['state'], seconds=time.monotonic()-stage['started_at_monotonic'],
            reservations=[{'id': str(row['id']), 'state': row['state'], 'units': row['units']} for row in fresh],
            usage=[{'reservation_id': str(row['reservation_id']), 'units': row['units'], 'details': row['details']}
                   for row in usage if row['reservation_id'] in {row['id'] for row in fresh}], workers=workers)
        requests = sum(row['usage'].get('request_count', 0) for row in workers)
        stage['requests'] = requests
        stage['reservation_units'] = sum(row['units'] for row in fresh)
        stage['source_post_delta'] = sum(row['method'] == 'POST' for row in self.site.snapshot()['requests'])-source_before
        if seed:
            self.report['mechanical_seed'] = stage
        else:
            self.report['stages'].append(stage)
        self.report.pop('active_stage', None)
        self.persist()
        verify_run_spend(self.run_allowance, current, usage)
        if (result['outcome'] != expected or len(fresh) != (0 if seed else 1)
                or requests > stage['request_ceiling'] or stage['reservation_units'] > (0 if seed else 40000)
                or stage['seconds'] > 180 or any(row['state'] != 'settled' for row in fresh)
                or any(row['usage'].get('billing_unknown') or row['usage'].get('unmeasured_requests')
                       or row['usage'].get('credential_literal_seen') for row in workers)):
            raise AcceptanceStop('bounded_stage_failed:' + name)
        if stage['source_post_delta'] != (1 if name in ('login', 'publish', 'reply', 'unknown_seed') else 0):
            raise AcceptanceStop('unexpected_source_post_count:' + name)
        if not seed and (not workers or sum(row['usage'].get('total_tokens', 0) for row in workers) > 40000):
            raise AcceptanceStop('missing_or_excess_usage:' + name)
        if self.require_visual and not seed and any(
                type(row['usage'].get('vision_images_sent')) is not int
                or not 1 <= row['usage']['vision_images_sent'] <= row['usage'].get('request_count', 0)
                for row in workers):
            raise AcceptanceStop('visual_image_missing_or_invalid:' + name)
        if sum(row['requests'] for row in self.report['stages']) > 48:
            raise AcceptanceStop('case_request_limit')
        return result

    def submit(self, kind, purpose, payload, *, target=None):
        from lead_app.db import tx
        from lead_app.growth.contracts import TaskSpec, utcnow
        from lead_app.growth.store import submit_work
        with tx(self.aid) as c:
            return submit_work(c, self.aid, TaskSpec(kind=kind, purpose=purpose,
                account_id=self.pending['account_id'], target_id=target,
                payload=payload, review_at=utcnow()+timedelta(hours=1)))

    def content(self, text, *, operation='publish', parent=None):
        from lead_app.db import j, one, tx
        from lead_app.growth.store import digest
        from lead_app.growth.tools import ChannelInput
        body = {'body': text, 'title': '', 'format': 'text', 'media': []}
        with tx(self.aid) as c:
            row = one(c, '''INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                VALUES(lead.ctx_tenant(),%s,1,%s,'[]',%s,%s) RETURNING *''',
                (self.aid, j(body), j({'acceptance_frozen_evidence': digest(body)}), digest(body)))
        data = ChannelInput(content_id=row['id'], content_hash=row['content_hash'], operation=operation, parent_url=parent)
        return self.submit('channel', operation, data.model_dump(mode='json'), target=self.target_id)

    def action(self, ref):
        from lead_app.db import all_rows, one, tx
        with tx(self.aid) as c:
            action = one(c, 'SELECT * FROM lead.actions WHERE task_id=%s', (ref['task_id'],))
            attempts = all_rows(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s ORDER BY attempt', (action['id'],))
            valid = one(c, 'SELECT lead.growth_receipt_valid(%s) ok', (action['id'],))['ok']
        return action, attempts, valid

    async def confirmed(self, ref, text, parent=None):
        import httpx
        from bs4 import BeautifulSoup
        from lead_app import browser
        from lead_app.db import one, tx
        action, attempts, valid = self.action(ref)
        if action['state'] != 'confirmed' or len(attempts) != 1 or not valid:
            raise AcceptanceStop('original_receipt_not_confirmed')
        receipt = attempts[0]['observation']['browser_receipt']
        snapshot = self.site.snapshot()
        matching = [post for post in snapshot['posts'] if post['url'] == action['external_url']
                    and post['author'] == self.site.expected_subject and post['body'] == text and post.get('parent_url') == parent]
        if len(matching) != 1:
            raise AcceptanceStop('independent_source_receipt_mismatch')
        with tx(self.aid) as c:
            account = one(c, 'SELECT profile_id FROM lead.social_accounts WHERE id=%s', (self.pending['account_id'],))
        actor = await browser.gateway.get(self.aid, account['profile_id'])
        cookies = await actor.context.cookies(action['external_url'])
        parsed = urlsplit(action['external_url'])
        if parsed.scheme+'://'+parsed.netloc != self.site.origin:
            raise AcceptanceStop('receipt_outside_synthetic_site')
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            response = await client.get('http://127.0.0.1:'+str(self.site.listener_port)+parsed.path,
                headers={'Host': parsed.netloc, 'Cookie': '; '.join(row['name']+'='+row['value'] for row in cookies)})
        response.raise_for_status()
        dom = BeautifulSoup(response.text, 'html.parser')
        selectors = receipt['selectors']
        nodes = {key: dom.select(selector) for key, selector in selectors.items()}
        if (any(len(rows) != 1 for rows in nodes.values())
                or nodes['author'][0].get_text(strip=True) != self.site.expected_subject
                or nodes['body'][0].get_text(strip=True) != text
                or urljoin(action['external_url'], nodes['url'][0].get('href', '')) != action['external_url']
                or parent and urljoin(action['external_url'], nodes['parent'][0].get('href', '')) != parent):
            raise AcceptanceStop('independent_actual_dom_receipt_mismatch')
        return {'action_id': str(action['id']), 'attempt_id': str(attempts[0]['id']), 'attempt': 1,
                'url': action['external_url'], 'parent_url': parent, 'source_receipt': True,
                'independent_dom_receipt': True, 'body_hash': sha(text.encode())}

    async def seed(self, text):
        """Use the learned method via native tools, without an Agent/model call."""
        from lead_app import native_runner
        from lead_app.config import ROOT, env
        from lead_app.security import internal_token
        if self.run_allowance is not None:
            require_run_allowance(self.run_allowance, *self.ledger())
        self.site.arm_drop_ack()
        ref = self.content(text)
        original = native_runner.run_task

        async def mechanical(controller, endpoint, **_):
            scope = await controller.start()
            await asyncio.to_thread(controller.start_fragment)
            runtime = Path(env().get('LEAD_BROWSER_RUNTIME_DIR') or ROOT/'services/browser-runtime')
            data = {'scope': scope, 'agent_id': self.aid, 'task_id': controller.task_id,
                    'gateway_endpoint': endpoint, 'gateway_token': internal_token(),
                    'method': controller.action['intent']['browser_method'], 'body': controller.action['intent']['body']}
            child = await asyncio.create_subprocess_exec(env()['LEAD_BROWSER_RUNTIME_PYTHON'], '-B', '-S',
                str(ROOT/'scripts/native_eval_seed.py'), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={key: value for key, value in {**os.environ,
                    'PYTHONPATH': str(runtime/'site-packages')+os.pathsep+str(ROOT/'services/api'),
                    'ANONYMIZED_TELEMETRY': 'false', 'BROWSER_USE_CLOUD_SYNC': 'false',
                    'BROWSER_USE_VERSION_CHECK': 'false', 'BROWSER_USE_SETUP_LOGGING': 'false'}.items()
                    if key != 'BROWSER_USE_API_KEY' and
                    (key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'PYTHONPATH') or key.startswith(('ANONYMIZED_', 'BROWSER_USE_')))})
            try:
                stdout, _ = await asyncio.wait_for(child.communicate(json.dumps(data).encode()), 90)
                if child.returncode or json.loads(stdout).get('model_requests') != 0:
                    raise AcceptanceStop('mechanical_seed_worker_failed')
                return {'success': False, 'usage': {'request_count': 0}}
            finally:
                if child.returncode is None:
                    child.kill()
                    await asyncio.wait_for(child.communicate(), 5)

        native_runner.run_task = mechanical
        try:
            await self.perform('unknown_seed', ref, expected='unknown', seed=True)
        finally:
            native_runner.run_task = original
        action, attempts, _ = self.action(ref)
        if action['state'] != 'unknown' or len(attempts) != 1 or not attempts[0]['observation']['browser_request']['claimed']:
            raise AcceptanceStop('seed_did_not_create_real_unknown')
        self.report['mechanical_seed'].update(action_id=str(action['id']), attempt_id=str(attempts[0]['id']))
        return ref

    async def run(self, evidence):
        from lead_app import browser
        from lead_app.account_connection import begin
        from lead_app.connection_control import LoginInput
        from lead_app.db import one, tx
        from lead_app.growth.contracts import utcnow
        try:
            if set(evidence) != {'publish', 'reply', 'unknown'} or any(not isinstance(value, str) or not value for value in evidence.values()):
                raise AcceptanceStop('fixed_content_evidence_required')
            self.report['content_evidence'] = {key: sha(value.encode()) for key, value in evidence.items()}
            self.pending = await begin(self.aid, 'web', site_url=self.site.entry_url,
                login=LoginInput(username=self.site.username, password=self.site.password))
            with tx(self.aid) as c:
                task = one(c, "SELECT * FROM lead.tasks WHERE input#>>'{spec,payload,takeover_id}'=%s", (str(self.pending['takeover_id']),))
            ref = {'agent_id': self.aid, 'task_id': task['id'], 'task_generation': task['input']['task_generation']}
            await self.perform('login', ref)
            with tx(self.aid) as c:
                account = one(c, 'SELECT * FROM lead.social_accounts WHERE id=%s', (self.pending['account_id'],))
            if account['external_account_id'] != self.site.expected_subject or account['state'] != 'connected':
                raise AcceptanceStop('wrong_or_unproven_private_subject')
            actor = await browser.gateway.get(self.aid, account['profile_id'])
            await actor.connection_pages[str(self.pending['takeover_id'])].reconcile()
            work = self.submit('account', 'discover', {'target_urls': [self.site.target_url]})
            learned = await self.perform('discover_publish', work)
            targets = learned['output'].get('targets', [])
            if len(targets) != 1 or targets[0].get('operation') != 'publish':
                raise AcceptanceStop('publish_method_not_learned')
            self.target_id = targets[0]['target_id']
            work = self.content(evidence['publish'])
            await self.perform('publish', work)
            self.report['stages'][-1]['receipt'] = await self.confirmed(work, evidence['publish'])
            parent = self.report['stages'][-1]['receipt']['url']
            work = self.submit('account', 'discover', {'target_urls': [parent]})
            learned = await self.perform('discover_reply', work)
            if len(learned['output'].get('targets', [])) != 1 or learned['output']['targets'][0].get('operation') != 'reply':
                raise AcceptanceStop('reply_method_not_learned')
            work = self.content(evidence['reply'], operation='reply', parent=parent)
            await self.perform('reply', work)
            self.report['stages'][-1]['receipt'] = await self.confirmed(work, evidence['reply'], parent)
            unknown = await self.seed(evidence['unknown'])
            posts = len(self.site.snapshot()['posts'])
            # Only the fixture clock is accelerated past the original bounded
            # read wait. No task/attempt/deadline/budget field is rewritten.
            self.report['reconcile_clock_advance_seconds'] = 360
            await self.perform('reconcile', unknown, now=utcnow()+timedelta(minutes=6))
            self.report['stages'][-1]['receipt'] = await self.confirmed(unknown, evidence['unknown'])
            if len(self.site.snapshot()['posts']) != posts:
                raise AcceptanceStop('unknown_action_resubmitted')
            self.report['completed'] = True
        except Exception as error:
            self.report['completed'] = False
            self.report['first_failure'] = str(error) if isinstance(error, AcceptanceStop) else type(error).__name__
            raise
        finally:
            self.persist()
        return self.report


async def execute_registered(prereg, config, output):
    """Load only explicitly registered synthetic adapters after freeze checks."""
    import socket

    import uvicorn
    from lead_app import browser, domain
    from lead_app.config import ROOT, env, model_config
    from lead_app.db import tx

    values = env()
    if values.get('LEAD_DEPLOYMENT_MODE') == 'production' or values.get('LEAD_DB_HOST') not in ('127.0.0.1', 'localhost'):
        raise ValueError('isolated_synthetic_database_required')
    if not (values.get('LEAD_DB_NAME') or '').startswith('native_eval_'):
        raise ValueError('dedicated_native_eval_database_required')
    if values.get('LEAD_NETWORK_NAMESPACE') != '1':
        raise ValueError('actual_namespace_required')
    runtime = ROOT/'services/browser-runtime'
    if sha((runtime/'manifest.json').read_bytes()) != prereg['runtime_manifest_sha256']:
        raise ValueError('runtime_bundle_hash_mismatch')
    actual = model_config()
    if not actual.get('key') or {key: actual[key] for key in ('provider', 'model', 'base_url')} != config['model']:
        raise ValueError('configured_development_model_mismatch')
    if sha(actual['key'].encode()) != prereg.get('provider_credential_sha256'):
        raise ValueError('registered_provider_credential_mismatch')
    sites, aid, server, serving = [], None, None, None
    transports = ExitStack()
    previous = os.environ.get('LEAD_BROWSER_TRANSPORTS')
    report = {'source_hashes': prereg['source_hashes'], 'cases': [], 'first_failure': None}
    try:
        for spec in config['sites']:
            module, function = spec['adapter'].split(':', 1)
            sites.append(getattr(importlib.import_module(module), function)(**spec['kwargs']))
        transports.enter_context(synthetic_site_transport(sites))
        with socket.socket() as chooser:
            chooser.bind(('127.0.0.1', 0))
            port = chooser.getsockname()[1]
        endpoint = 'http://127.0.0.1:'+str(port)
        os.environ['LEAD_BROWSER_TRANSPORTS'] = json.dumps({'shared': endpoint})
        server = uvicorn.Server(uvicorn.Config(browser.app, host='127.0.0.1', port=port, log_level='error'))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(.05)
        aid = domain.create_agent(runtime_engine='growth')
        with tx(aid) as c:
            c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        for site, spec in zip(sites, config['sites'], strict=True):
            case = NativeCase(aid, site, endpoint, output=Path(output).with_name(site.case_id+'.json'),
                run_allowance=prereg.get('execution_allowance'),
                require_visual=prereg.get('final_visual_trial') is True)
            try:
                report['cases'].append(await case.run(spec['content_evidence']))
            except Exception:
                report['cases'].append(case.report)
                raise
    except Exception as error:
        report['first_failure'] = type(error).__name__
        raise
    finally:
        private_write(output, report)
        if aid:
            await browser.gateway.close(aid)
            domain.set_control(aid, 'archive')
        if server:
            server.should_exit = True
            await serving
        for site in sites:
            site.close()
        transports.close()
        if previous is None:
            os.environ.pop('LEAD_BROWSER_TRANSPORTS', None)
        else:
            os.environ['LEAD_BROWSER_TRANSPORTS'] = previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-prereg', type=Path, default=Path('native-c22-prereg.json'))
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--prereg', type=Path)
    parser.add_argument('--prereg-sha256')
    parser.add_argument('--private-config', type=Path)
    parser.add_argument('--output', type=Path, default=Path('native-c22-results.json'))
    args = parser.parse_args()
    if not args.execute:
        private_write(args.write_prereg, plan())
        print(json.dumps({'state': 'draft', 'path': str(args.write_prereg), 'model_requests': 0}))
        return
    if not all((args.prereg, args.prereg_sha256, args.private_config)):
        parser.error('--execute requires --prereg, --prereg-sha256 and --private-config')
    prereg, config = validate_prereg(args.prereg, args.private_config, args.prereg_sha256)
    asyncio.run(execute_registered(prereg, config, args.output))


if __name__ == '__main__':
    main()
