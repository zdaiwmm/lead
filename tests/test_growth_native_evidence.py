"""Native source reads become scoped source documents, never business outcomes."""
import socket
from datetime import timedelta

import pytest
from lead_app import domain, fetch, local_executor
from lead_app.account_intents import set_intent
from lead_app.db import j, one, tx
from lead_app.growth.contracts import EvidenceRef, ExperimentSpec, TaskSpec, utcnow
from lead_app.growth.runtime import advance
from lead_app.growth.store import create_experiment, submit_work
from test_executor_nodes import connected as connected_node
from test_executor_nodes import fence, new_user, scope_for
from test_growth_account_connection import account
from test_growth_runtime import new_work
from test_local_executor import connected

SOURCE = {'url': 'https://forum.example/original', 'title': 'An original need',
          'text': 'I need a private way to invite a friend to a quiet room.', 'links': []}


@pytest.fixture
def native_owner():
    from lead_app.scope import tenant_scope
    owner = new_user()
    with tenant_scope(owner['tenant_id'], owner):
        yield owner


def request_for(aid, *, device=None, account_id=None):
    new_work(aid)
    device = device or connected(aid)
    with tx(aid) as c:
        ref = submit_work(c, aid, TaskSpec(kind='decision', purpose='growth',
            account_id=account_id, payload={'triggers': []}, review_at=utcnow() + timedelta(hours=1)))
    assert advance(ref)['state'] == 'ready'
    request = local_executor.submit(aid, 'native-source', {
        'input': 'Read original evidence', 'instructions': 'Use actual sources.',
        'tools': [{'name': 'lead_public_read'}, {'name': 'lead_document_read'}],
        'tool_context': {'kind': 'model', 'task_id': ref['task_id']}},
        session_key='native-source', task_id=ref['task_id'])
    return device, request


async def test_growth_read_without_browser_returns_retained_document(growth_agent, monkeypatch):
    device, request = request_for(growth_agent)
    async def read(url, **kwargs):
        return dict(SOURCE)
    monkeypatch.setattr(fetch, 'fetch_product', read)
    result = await local_executor.call_tool(device['token'], device['epoch'], request['id'],
        'lead_public_read', {'url': SOURCE['url']})
    observed = result['structuredContent']
    assert observed['evidence_ref']['kind'] == 'document'
    with tx(growth_agent) as c:
        document = one(c, 'SELECT * FROM lead.knowledge_documents WHERE id=%s',
                       (observed['evidence_ref']['id'],))
        assert document['source_ref'] == SOURCE['url']
        assert document['trust_state'] == 'untrusted_source'
        assert one(c, 'SELECT count(*) n FROM lead.browser_runs')['n'] == 0
    reread = await local_executor.call_tool(device['token'], device['epoch'], request['id'],
        'lead_document_read', {'id': observed['evidence_ref']['id']})
    assert reread['structuredContent']['chunks'][0]['body'] == SOURCE['text']


async def test_claimed_draft_work_keeps_the_runtime_read_contract(growth_agent, monkeypatch):
    device, request = request_for(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET desired_state='draft' WHERE id=%s",(growth_agent,))
    async def read(url, **kwargs):
        return dict(SOURCE)
    monkeypatch.setattr(fetch,'fetch_product',read)
    result=await local_executor.call_tool(device['token'],device['epoch'],request['id'],
        'lead_public_read',{'url':SOURCE['url']})
    assert result['structuredContent']['evidence_ref']['kind']=='document'


@pytest.mark.parametrize('change', ['control', 'generation', 'attempt', 'canceled', 'request', 'device', 'pause', 'lease'])
async def test_late_read_after_fence_change_cannot_register_evidence(growth_agent, monkeypatch, change):
    device, request = request_for(growth_agent)
    async def read(url, **kwargs):
        with tx(growth_agent) as c:
            if change == 'control':
                c.execute('UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s', (growth_agent,))
            elif change == 'generation':
                c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{task_generation}','2') WHERE id=%s", (request['task_id'],))
            elif change == 'attempt':
                c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,attempt_token}',%s) WHERE id=%s", (j('new-attempt'), request['task_id']))
            elif change == 'canceled':
                c.execute("UPDATE lead.tasks SET state='canceled' WHERE id=%s", (request['task_id'],))
            elif change == 'request':
                c.execute("UPDATE lead.local_executor_requests SET state='canceled' WHERE id=%s", (request['id'],))
            elif change == 'device':
                c.execute('UPDATE lead.local_executor_devices SET epoch=epoch+1 WHERE id=%s', (request['device_id'],))
            elif change == 'lease':
                c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,lease_until}',%s) WHERE id=%s",
                    (j((utcnow()-timedelta(seconds=1)).isoformat()), request['task_id']))
            else:
                c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s", (growth_agent,))
        return dict(SOURCE)
    monkeypatch.setattr(fetch, 'fetch_product', read)
    with pytest.raises((ValueError, LookupError), match='executor_|scope_changed'):
        await local_executor.call_tool(device['token'], device['epoch'], request['id'],
            'lead_public_read', {'url': SOURCE['url']})
    with tx(growth_agent) as c:
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == 0


@pytest.mark.parametrize('page', [
    {**SOURCE, 'title': 'Access denied'},
    {**SOURCE, 'title': 'Log in'},
    {**SOURCE, 'title': 'Just a moment...', 'text': 'Please complete the following challenge'},
    {**SOURCE, 'text': ''},
    {**SOURCE, 'requires_javascript': True},
    {**SOURCE, 'url': 'https://www.google.com/search?q=quiet+room'},
    {**SOURCE, 'url': 'https://html.duckduckgo.com/html/?q=quiet+room'},
    {**SOURCE, 'url': 'https://github.com/search?type=issues&q=quiet+room'},
])
async def test_unavailable_and_search_pages_are_not_source_documents(growth_agent, monkeypatch, page):
    device, request = request_for(growth_agent)
    async def read(url, **kwargs):
        return dict(page)
    monkeypatch.setattr(fetch, 'fetch_product', read)
    result = await local_executor.call_tool(device['token'], device['epoch'], request['id'],
        'lead_public_read', {'url': page['url']})
    assert not result['structuredContent'].get('evidence_ref')
    with tx(growth_agent) as c:
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == 0


async def test_rereading_old_source_cannot_reopen_exhausted_question(growth_agent, monkeypatch):
    device, request = request_for(growth_agent)
    current = dict(SOURCE)
    async def read(url, **kwargs):
        return dict(current)
    monkeypatch.setattr(fetch, 'fetch_product', read)
    async def observe():
        result = await local_executor.call_tool(device['token'], device['epoch'], request['id'],
            'lead_public_read', {'url': SOURCE['url']})
        return result['structuredContent']
    first = await observe()
    spec = ExperimentSpec(question_type='audience', question='Who needs quiet rooms?',
        hypothesis='Friends need private rooms', learning_signal='An original need', token_limit=1000,
        review_at=utcnow() + timedelta(hours=1))
    with tx(growth_agent) as c:
        for variation in ('first', 'second'):
            create_experiment(c, growth_agent, spec.model_copy(update={'variation': variation}))
        c.execute("UPDATE lead.growth_experiments SET state='closed',closed_at=now(),conclusion=%s",
                  (j({'verified_progress': False}),))
        created = one(c, 'SELECT created_at FROM lead.knowledge_documents WHERE id=%s', (first['evidence_ref']['id'],))['created_at']
    duplicate = await observe()
    assert duplicate['evidence_ref'] == first['evidence_ref']
    assert duplicate['knowledge_progress'] is False
    with tx(growth_agent) as c:
        assert one(c, 'SELECT created_at FROM lead.knowledge_documents WHERE id=%s', (first['evidence_ref']['id'],))['created_at'] == created
        with pytest.raises(ValueError, match='no_progress_path_exhausted'):
            create_experiment(c, growth_agent, spec.model_copy(update={'variation': 'third', 'evidence_refs': [EvidenceRef.model_validate(first['evidence_ref'])]}))
    current['text'] = 'A newly observed original request from a different friend.'
    fresh = await observe()
    assert fresh['evidence_ref'] != first['evidence_ref']
    with tx(growth_agent) as c:
        accepted = create_experiment(c, growth_agent, spec.model_copy(update={'variation': 'third', 'evidence_refs': [EvidenceRef.model_validate(fresh['evidence_ref'])]}))
        assert accepted['state'] == 'active'


@pytest.mark.parametrize('bound', [True, False])
async def test_account_pause_only_fences_reads_bound_to_that_account(growth_agent, monkeypatch, bound):
    sid = account(growth_agent)
    set_intent(growth_agent, sid, 'running', source='owner_command', source_ref='native:running')
    device, request = request_for(growth_agent, account_id=sid if bound else None)
    async def read(url, **kwargs):
        set_intent(growth_agent, sid, 'paused', source='owner_command', source_ref='native:pause')
        return dict(SOURCE)
    monkeypatch.setattr(fetch, 'fetch_product', read)
    async def invoke():
        return await local_executor.call_tool(device['token'], device['epoch'], request['id'],
            'lead_public_read', {'url': SOURCE['url']})
    if bound:
        with pytest.raises(ValueError, match='executor_scope_changed'):
            await invoke()
    else:
        assert (await invoke())['structuredContent']['evidence_ref']['kind'] == 'document'


@pytest.mark.parametrize('change,status', [(None, None), ('grant', 403), ('node', 401), ('epoch', 401), ('task', 403), ('control', 403)])
async def test_managed_read_rechecks_real_grant_after_fetch(native_owner, monkeypatch, change, status):
    from types import SimpleNamespace
    from uuid import UUID

    from fastapi import HTTPException
    from lead_app import executor_nodes as nodes
    from lead_app.executor_nodes_api import ScopeTools, scope_tools
    from lead_app.scope import tenant_scope

    owner = native_owner
    node, _ = connected_node()
    aid = domain.create_agent(runtime_engine='growth')
    scope = scope_for(node, aid)
    _, request = request_for(aid, device={'managed': True})
    async def read(url, **kwargs):
        if change in {'grant', 'node', 'epoch'}:
            with tenant_scope(owner['tenant_id'], owner):
                if change == 'grant':
                    nodes.revoke_grant(scope['scope_ref'])
                else:
                    nodes.control_node(node['node_id'], 'revoke' if change == 'node' else 'rotate')
        elif change:
            with tx(aid) as c:
                if change == 'task':
                    c.execute("UPDATE lead.tasks SET state='canceled' WHERE id=%s", (request['task_id'],))
                else:
                    c.execute('UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s', (aid,))
        return dict(SOURCE)
    monkeypatch.setattr(fetch, 'fetch_product', read)
    body = fence(node, scope, ScopeTools, request_id=str(request['id']),
                 name='lead_public_read', arguments={'url': SOURCE['url']})
    async def invoke():
        return await scope_tools(UUID(scope['scope_ref']),
            SimpleNamespace(headers={'authorization': 'Bearer ' + node['node_token']}), body)
    if change:
        with pytest.raises(HTTPException) as error:
            await invoke()
        assert error.value.status_code == status
    else:
        assert (await invoke())['structuredContent']['evidence_ref']['kind'] == 'document'
    with tx(aid) as c:
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == (0 if change else 1)
    domain.set_control(aid, 'archive')


async def test_public_chat_without_task_keeps_read_only_behavior(growth_agent, monkeypatch):
    device = connected(growth_agent)
    request = local_executor.submit(growth_agent, 'chat-source', {
        'input': 'Read original evidence', 'instructions': 'Use sources.',
        'tools': [{'name': 'lead_public_read'}], 'tool_context': {'kind': 'model'}}, session_key='chat-source')
    async def read(url, **kwargs):
        return dict(SOURCE)
    monkeypatch.setattr(fetch, 'fetch_product', read)
    result = await local_executor.call_tool(device['token'], device['epoch'], request['id'],
        'lead_public_read', {'url': SOURCE['url']})
    assert result['structuredContent']['text'] == SOURCE['text']
    assert 'evidence_ref' not in result['structuredContent']
    with tx(growth_agent) as c:
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == 0


@pytest.mark.parametrize('change', [None, 'grant', 'task'])
async def test_managed_source_error_still_rechecks_original_authority(native_owner, monkeypatch, change):
    from types import SimpleNamespace
    from uuid import UUID

    from fastapi import HTTPException
    from lead_app import executor_nodes as nodes
    from lead_app.executor_nodes_api import ScopeTools, scope_tools
    from lead_app.scope import tenant_scope

    node, _ = connected_node()
    aid = domain.create_agent(runtime_engine='growth')
    scope = scope_for(node, aid)
    _, request = request_for(aid, device={'managed': True})
    async def unavailable(url, **kwargs):
        if change == 'grant':
            with tenant_scope(native_owner['tenant_id'], native_owner):
                nodes.revoke_grant(scope['scope_ref'])
        elif change == 'task':
            with tx(aid) as c:
                c.execute("UPDATE lead.tasks SET state='canceled' WHERE id=%s", (request['task_id'],))
        raise socket.gaierror(-3, 'test-only DNS failure')
    monkeypatch.setattr(fetch, 'fetch_product', unavailable)
    body = fence(node, scope, ScopeTools, request_id=str(request['id']),
        name='lead_public_read', arguments={'url': SOURCE['url']})
    async def invoke():
        return await scope_tools(UUID(scope['scope_ref']),
            SimpleNamespace(headers={'authorization': 'Bearer ' + node['node_token']}), body)
    if change:
        with pytest.raises(HTTPException) as error:
            await invoke()
        assert error.value.status_code == 403
    else:
        result = await invoke()
        assert result['isError'] and result['structuredContent']['reason'] == 'source_dns_unavailable'
    with tx(aid) as c:
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == 0
    domain.set_control(aid, 'archive')


async def test_committed_account_pause_fences_before_task_projection(growth_agent, monkeypatch):
    from lead_app.account_intents import auth_scope
    from lead_app.identity_store import auth_tx

    sid = account(growth_agent)
    set_intent(growth_agent, sid, 'running', source='owner_command', source_ref='native:initial')
    device, request = request_for(growth_agent, account_id=sid)
    async def read(url, **kwargs):
        # Model the real interval between the authoritative intent commit and
        # the later task-cancellation projection, without inventing another writer.
        with auth_tx() as c:
            auth_scope(c, growth_agent)
            c.execute("SELECT lead.growth_set_intent(%s,%s,'paused','owner_command',%s,NULL,NULL)",
                      (growth_agent, sid, 'native:before-projection'))
        with tx(growth_agent) as c:
            assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (request['task_id'],))['state'] == 'running'
        return dict(SOURCE)
    monkeypatch.setattr(fetch, 'fetch_product', read)
    with pytest.raises(ValueError, match='executor_scope_changed'):
        await local_executor.call_tool(device['token'], device['epoch'], request['id'],
            'lead_public_read', {'url': SOURCE['url']})
    with tx(growth_agent) as c:
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == 0


async def test_search_provider_original_document_is_not_confused_with_index(growth_agent, monkeypatch):
    device, request = request_for(growth_agent)
    async def read(url, **kwargs):
        return {**SOURCE, 'url': 'https://duckduckgo.com/privacy'}
    monkeypatch.setattr(fetch, 'fetch_product', read)
    result = await local_executor.call_tool(device['token'], device['epoch'], request['id'],
        'lead_public_read', {'url': 'https://duckduckgo.com/privacy'})
    assert result['structuredContent']['evidence_ref']['kind'] == 'document'


async def test_registered_document_is_not_readable_from_another_project(growth_agent, monkeypatch):
    device, request = request_for(growth_agent)
    async def read(url, **kwargs):
        return dict(SOURCE)
    monkeypatch.setattr(fetch, 'fetch_product', read)
    result = await local_executor.call_tool(device['token'], device['epoch'], request['id'],
        'lead_public_read', {'url': SOURCE['url']})
    other = domain.create_agent(runtime_engine='growth')
    foreign_device, foreign_request = request_for(other)
    with pytest.raises(LookupError, match='source_unavailable'):
        await local_executor.call_tool(foreign_device['token'], foreign_device['epoch'], foreign_request['id'],
            'lead_document_read', {'id': result['structuredContent']['evidence_ref']['id']})
