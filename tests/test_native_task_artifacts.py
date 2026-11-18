"""Retained content must cross the real scoped read and revocation barriers."""
import json
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import HTTPException
from lead_app import domain, executor_nodes_api, fetch
from lead_app import executor_nodes as nodes
from lead_app import local_executor as core
from lead_app import local_executor_tools as tools
from lead_app.config import database_kwargs
from lead_app.db import j, one, tx
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.store import digest, submit_work
from lead_app.growth.tools import _remember
from lead_app.scope import tenant_scope
from starlette.requests import Request
from test_executor_nodes import connected, fence, new_user, poll, publish, scope_for
from test_local_executor import connected as personal

NAMES = ('lead_task_read', 'lead_document_read', 'lead_public_read')


def save(aid, text='A useful factual draft.'):
    with tx(aid) as c:
        ref = submit_work(c, aid, TaskSpec(kind='content', purpose='retained-draft',
            payload={'brief': 'Prepare a factual draft'}, dedupe_key=str(uuid4()), review_at=utcnow()+timedelta(hours=1)))
        body = {'title': 'Draft title', 'body': text, 'format': 'text', 'media': [], 'trust': 'generated'}
        validation = {'publication_status': 'draft', 'optional_media_missing': False,
            'business_profile_id': None, 'growth_task_id': ref['task_id'], 'claim_checks': []}
        sources = [{'kind': 'task', 'id': ref['task_id']}]
        item = one(c, """INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,
            validation_result,content_hash) VALUES(lead.ctx_tenant(),%s,1,%s,%s,%s,%s) RETURNING id""",
            (aid, j(body), j(sources), j({**validation, 'private_token': 'MUST_NOT_ESCAPE'}), digest(body)))
        artifact = {'kind': 'content', 'id': str(item['id']), 'revision': 1, 'hash': digest(body)}
        result = {'outcome': 'completed', 'artifacts': [artifact], 'output': {'quality_status': 'draft'}}
        c.execute("UPDATE lead.tasks SET state='succeeded',completed_at=now(),result=%s WHERE id=%s",
                  (j({'runtime': {}, 'output': result}), ref['task_id']))
    return {'aid': aid, 'task': ref['task_id'], 'artifact': artifact, 'body': body,
            'sources': sources, 'validation': validation}


def replace_refs(sample, refs):
    with tx(sample['aid']) as c:
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{output,artifacts}',%s) WHERE id=%s",
                  (j(refs), sample['task']))


@pytest.fixture
def sample():
    owner = new_user()
    with tenant_scope(owner['tenant_id'], owner):
        publish('legacy')
        yield {**save(domain.create_agent(runtime_engine='growth')), 'owner': owner}


def read(sample):
    return tools._business_read({'agent_id': sample['aid']}, 'lead_task_read', sample['task'])


def session(sample, mode, workspace=False):
    aid = sample['aid']
    payload = {'input': 'Inspect retained work', 'instructions': 'Read only',
        'tool_context': {'kind': 'model', 'workspace_request': workspace},
        'tools': [{'name': n, 'description': 'Read', 'inputSchema': {'type': 'object'}} for n in NAMES]}
    if mode == 'personal':
        device = personal(aid)
        request = core.submit(aid, str(uuid4()), payload, session_key=str(uuid4()))
        async def invoke(name, arguments):
            return await core.call_tool(device['token'], device['epoch'], request['id'], name, arguments)
        def revoke():
            core.revoke(aid, device['device_id'])
    else:
        node, _ = connected()
        scope = scope_for(node, aid)
        poll(node, scope)
        request = core.submit(aid, str(uuid4()), payload, session_key=str(uuid4()))
        async def invoke(name, arguments):
            body = fence(node, scope, executor_nodes_api.ScopeTools, request_id=request['id'], name=name, arguments=arguments)
            http = Request({'type': 'http', 'headers': [(b'authorization', ('Bearer '+node['node_token']).encode())]})
            return await executor_nodes_api.scope_tools(UUID(scope['scope_ref']), http, body)
        def revoke():
            nodes.revoke_grant(scope['scope_ref'])
    return invoke, revoke


def test_retained_body_and_validation_are_complete_scoped_data(sample):
    result = read(sample)
    assert {'id', 'type', 'state', 'specification', 'output'} <= result.keys()
    assert result['output']['artifacts'] == [sample['artifact']]
    assert result['resolved_artifacts'] == [{**sample['artifact'], 'available': True, 'body': sample['body'],
        'source_refs': sample['sources'], 'validation': sample['validation']}]
    assert 'MUST_NOT_ESCAPE' not in json.dumps(result, default=str)


@pytest.mark.parametrize('field,value', [pytest.param('id', str(uuid4()), id='missing-content-id'), ('id', 'bad-id'), ('id', {'injected': True}), ('revision', 2),
    ('revision', True), ('revision', '1'), ('hash', 'f'*64), ('hash', {'malicious': True})])
def test_unavailable_references_remain_normal_task_results(sample, field, value):
    replace_refs(sample, [{**sample['artifact'], field: value}])
    result = read(sample)
    assert result['state'] == 'succeeded' and result['resolved_artifacts'][0]['available'] is False
    assert 'body' not in result['resolved_artifacts'][0]


def test_persisted_body_hash_drift_is_not_a_readable_artifact(sample):
    # Only corrupt this fixture's content through the isolated database owner;
    # the runtime correctly has no UPDATE privilege on retained content.
    with psycopg.connect(**database_kwargs(owner=True)) as c:
        c.execute("UPDATE lead.content_items SET body=jsonb_set(body,'{body}','\"tampered\"') WHERE id=%s",
                  (sample['artifact']['id'],))
    assert read(sample)['resolved_artifacts'][0]['available'] is False


@pytest.mark.parametrize('foreign_tenant', [False, True])
def test_content_from_another_scope_is_not_resolved(sample, foreign_tenant):
    if foreign_tenant:
        foreign = new_user()
        with tenant_scope(foreign['tenant_id'], foreign):
            other = save(domain.create_agent(runtime_engine='growth'), 'FOREIGN_CONTENT')
    else:
        other = save(domain.create_agent(runtime_engine='growth'), 'FOREIGN_CONTENT')
    replace_refs(sample, [other['artifact']])
    result = read(sample)
    assert result['resolved_artifacts'][0]['available'] is False
    assert 'FOREIGN_CONTENT' not in json.dumps(result, default=str)


def test_deduplicated_content_keeps_its_original_creator(sample):
    later = save(sample['aid'])
    replace_refs(later, [sample['artifact']])
    artifact = read(later)['resolved_artifacts'][0]
    assert artifact['available'] and artifact['validation']['growth_task_id'] == sample['task']
    assert artifact['body'] == sample['body']


def test_reference_count_is_bounded_without_changing_original_output(sample):
    references = [save(sample['aid'], 'Draft '+str(i))['artifact'] for i in range(17)]
    replace_refs(sample, references)
    result = read(sample)
    assert result['output']['artifacts'] == references
    assert len(result['resolved_artifacts']) <= 16 and result['resolved_artifacts_limited'] is True


@pytest.mark.parametrize('wide', [False, True])
def test_large_artifacts_are_not_silently_truncated(sample, wide):
    large = [save(sample['aid'], (('界' if wide else '')+str(i))*140000) for i in (1, 2)]
    replace_refs(sample, [item['artifact'] for item in large])
    result = read(sample)
    assert result['resolved_artifacts_limited'] is True
    available = [a for a in result['resolved_artifacts'] if a['available']]
    assert len(available) <= 1
    for item in available:
        assert item['body'] in [entry['body'] for entry in large]
    added = {k: v for k, v in result.items() if k.startswith('resolved_artifacts')}
    assert len(json.dumps(added, ensure_ascii=False, separators=(',', ':')).encode()) <= 256000


@pytest.mark.parametrize('mode', ['personal', 'managed'])
async def test_both_real_entrypoints_resolve_the_retained_content(sample, mode):
    invoke, _ = session(sample, mode)
    result = await invoke('lead_task_read', {'id': sample['task']})
    assert result['structuredContent']['resolved_artifacts'][0]['body'] == sample['body']


@pytest.mark.parametrize('mode', ['personal', 'managed'])
@pytest.mark.parametrize('name', NAMES[:2])
async def test_workspace_context_never_reads_project_business_data(sample, mode, name):
    invoke, _ = session(sample, mode, workspace=True)
    with pytest.raises((ValueError, HTTPException)) as error:
        await invoke(name, {'id': sample['task']})
    assert 'executor_tool_scope_changed' in str(error.value) or getattr(error.value, 'status_code', None) == 403


@pytest.mark.parametrize('mode', ['personal', 'managed'])
@pytest.mark.parametrize('name', NAMES)
async def test_revocation_during_each_read_fences_the_result(sample, mode, name, monkeypatch):
    invoke, revoke = session(sample, mode)
    observed = []
    def revoked():
        observed.append(True)
        with tenant_scope(sample['owner']['tenant_id'], sample['owner']):
            revoke()
    if name == 'lead_public_read':
        async def fetched(*args, **kwargs):
            revoked()
            return {'url': 'https://source.invalid/', 'title': 'Source', 'text': 'Observed source.', 'requires_javascript': False}
        monkeypatch.setattr(fetch, 'fetch_product', fetched)
        arguments = {'url': 'https://source.invalid/'}
    else:
        original = tools._business_read
        def completed(*args):
            value = original(*args)
            revoked()
            return value
        monkeypatch.setattr(tools, '_business_read', completed)
        reference = _remember(sample['aid'], {'url': 'https://source.invalid/', 'text': 'Observed source.'})[0].id if name == 'lead_document_read' else sample['task']
        arguments = {'id': str(reference)}
    with pytest.raises((ValueError, HTTPException)):
        await invoke(name, arguments)
    assert observed == [True]


def capture_content_rows(monkeypatch):
    from lead_app import db
    original, rows = db.one, []
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        if isinstance(result, dict) and {'body', 'source_refs'} <= result.keys():
            rows.append(json.dumps(result, ensure_ascii=False, default=str, separators=(',', ':')).encode())
        return result
    monkeypatch.setattr(db, 'one', observed)
    return rows


def test_oversized_source_refs_are_bounded_before_leaving_database(sample, monkeypatch):
    with psycopg.connect(**database_kwargs(owner=True)) as c:
        c.execute('UPDATE lead.content_items SET source_refs=%s WHERE id=%s',
                  (j([{'kind': 'task', 'id': sample['task'], 'claim': 'R'*300000}]), sample['artifact']['id']))
    rows = capture_content_rows(monkeypatch)
    result = read(sample)
    assert result['resolved_artifacts_limited'] is True
    assert not any(item['available'] for item in result['resolved_artifacts'])
    assert rows and max(map(len, rows)) <= 256000


def test_unrelated_large_validation_never_enters_api_memory(sample, monkeypatch):
    with psycopg.connect(**database_kwargs(owner=True)) as c:
        c.execute('UPDATE lead.content_items SET validation_result=validation_result || %s WHERE id=%s',
                  (j({'unrelated': 'UNRELATED_LARGE_VALIDATION'*16000}), sample['artifact']['id']))
    rows = capture_content_rows(monkeypatch)
    result = read(sample)
    artifact = result['resolved_artifacts'][0]
    assert artifact['available'] and artifact['body'] == sample['body']
    assert artifact['validation'] == sample['validation']
    assert rows and max(map(len, rows)) <= 256000
    assert all(b'UNRELATED_LARGE_VALIDATION' not in row for row in rows)
