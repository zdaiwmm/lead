"""Same-request native source reads join exact-quote research evidence."""
import json
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from lead_app import domain, fetch, local_executor, local_executor_client
from lead_app.config import database_kwargs
from lead_app.db import j, one, tx
from lead_app.growth import runtime, tools
from lead_app.growth.contracts import ExperimentSpec, TaskSpec, utcnow
from lead_app.growth.store import create_experiment, submit_work
from test_local_executor import connected

INITIAL = {'url': 'https://citation.example/catalog', 'title': 'Catalog',
           'text': 'This public catalog lists communication tools for independent makers.'}
NEW = {'url': 'https://citation.example/criteria', 'title': 'Criteria',
       'text': 'Our criteria require transparent ownership and an independently verifiable privacy policy.'}
QUOTE = 'Our criteria require transparent ownership'


async def native_research(aid, monkeypatch, *, reuse=False, mode='valid', read_tool='lead_public_read', dedupe=''):
    device = connected(aid)
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        experiment = create_experiment(c, aid, ExperimentSpec(question_type='audience', question='What are the source criteria?',
            hypothesis='The actual criteria answer the question.', learning_signal='An exact retained quotation.',
            token_limit=1000, review_at=utcnow()+timedelta(hours=1)))
        ref = submit_work(c, aid, TaskSpec(kind='research', purpose='original-criteria', experiment_id=experiment['id'],
            payload={'urls': [INITIAL['url']], 'question': 'What are the source criteria?'}, dedupe_key=dedupe,
            review_at=utcnow()+timedelta(hours=1)))
    retained = tools._remember(aid, NEW)[0] if reuse or read_tool == 'lead_document_read' or mode == 'unread' else None
    captured = {}

    async def read(url, **kwargs):
        return dict(INITIAL if url == INITIAL['url'] else NEW)

    monkeypatch.setattr(tools, 'fetch_product', read)
    monkeypatch.setattr(fetch, 'fetch_product', read)

    async def complete(agent_id, request_id):
        captured['request_id'] = request_id
        local_executor.events(device['token'], device['epoch'], [{'id': str(uuid4()), 'request_id': request_id,
            'method': 'request/started', 'params': {'thread_id': 'citation-thread', 'turn_id': 'citation-turn'}}])
        if mode == 'unread':
            identifier = str(retained.id)
        else:
            arguments = {'url': NEW['url']} if read_tool == 'lead_public_read' else {'id': str(retained.id)}
            if mode == 'redirect':
                arguments['url'] += '/requested'
            response = await local_executor.call_tool(device['token'], device['epoch'], request_id, read_tool, arguments)
            response = json.loads(json.dumps(response, default=str))
            view = response['structuredContent']
            identifier = str(view['document_id'] if read_tool == 'lead_public_read' else view['id'])
            captured['document_id'] = identifier
            item = {'id': 'actual-citation-read', 'type': 'mcpToolCall', 'server': 'lead', 'tool': read_tool,
                    'arguments': arguments, 'status': 'completed', 'result': response}
            params = {'threadId': 'citation-thread', 'turnId': 'citation-turn', 'item': item}
            if mode == 'thread':
                params['threadId'] = 'other-thread'
            if mode == 'turn':
                params['turnId'] = 'other-turn'
            if mode == 'failed':
                item['status'] = 'failed'
            if mode == 'error':
                item['result']['isError'] = True
            if mode == 'forged_body':
                item['result']['structuredContent']['text'] = 'A forged source body that was never retained.'
            if mode == 'server':
                item['server'] = 'foreign-server'
            event_id = str(uuid4())
            local_executor.events(device['token'], device['epoch'], [{'id': event_id, 'request_id': request_id,
                'method': 'item/completed', 'params': params}])
            if mode in ('request', 'task'):
                with tx(aid) as c:
                    original = one(c, 'SELECT * FROM lead.local_executor_requests WHERE id=%s', (request_id,))
                    other_task = submit_work(c, aid, TaskSpec(kind='research', purpose='another-task',
                        review_at=utcnow()+timedelta(hours=1))) if mode == 'task' else ref
                    other = one(c, """INSERT INTO lead.local_executor_requests(tenant_id,agent_id,device_id,task_id,
                        request_key,request_hash,session_key,payload,scope,thread_id,turn_id)
                        VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,%s,%s,'citation-thread','citation-turn') RETURNING id""",
                        (aid, device['device_id'], other_task['task_id'], 'other:'+str(uuid4()), original['request_hash'],
                         original['session_key'], j(original['payload']), j(original['scope'])))
                    c.execute('UPDATE lead.local_executor_events SET request_id=%s WHERE event_id=%s', (other['id'], event_id))
            if mode in ('body_hash', 'revision'):
                with tx(aid) as c:
                    c.execute('UPDATE lead.knowledge_documents SET '+('sha256=%s' if mode == 'body_hash' else 'revision=%s')+' WHERE id=%s',
                        ('f'*64 if mode == 'body_hash' else 2, identifier))
        if mode in ('agent', 'tenant'):
            if mode == 'tenant':
                from lead_app.scope import tenant_scope
                tenant = str(uuid4())
                with psycopg.connect(**database_kwargs(True)) as c:
                    c.execute("INSERT INTO lead.tenants(id,name) VALUES(%s,'Native citation isolation fixture')", (tenant,))
                with tenant_scope(tenant):
                    foreign = domain.create_agent(runtime_engine='growth')
                    identifier = str(tools._remember(foreign, NEW)[0].id)
                    domain.set_control(foreign, 'archive')
            else:
                foreign = domain.create_agent(runtime_engine='growth')
                identifier = str(tools._remember(foreign, NEW)[0].id)
        finding = {'document_id': identifier, 'quote': QUOTE if mode != 'quote' else QUOTE+' invented wording',
                   'finding': 'The actual criteria describe transparency.', 'relevance': 'It answers the original question.'}
        assessment = tools.ResearchAssessment(findings=[finding], unresolved=[]).model_dump(mode='json')
        with tx(aid) as c:
            request = one(c, 'SELECT payload FROM lead.local_executor_requests WHERE id=%s', (request_id,))
        output = {'result_json': json.dumps(assessment)} if request['payload']['result_codec'] == 'json_string_v1' else assessment
        local_executor.events(device['token'], device['epoch'], [{'id': str(uuid4()), 'request_id': request_id,
            'method': 'result', 'params': {'state': 'completed', 'output': output,
                'thread_id': 'citation-thread', 'turn_id': 'citation-turn'}}])
        if mode == 'generation':
            with tx(aid) as c:
                c.execute("UPDATE lead.local_executor_requests SET scope=jsonb_set(scope,'{task_generation}','2') WHERE id=%s", (request_id,))
        if mode in ('configuration', 'control', 'goal', 'paused'):
            with tx(aid) as c:
                if mode == 'configuration':
                    domain.snapshot_preferences(c, aid)
                elif mode == 'control':
                    c.execute('UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s', (aid,))
                elif mode == 'goal':
                    c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{operating_contract}','{\"revision\":2}') WHERE id=%s", (aid,))
                else:
                    c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s", (aid,))
        if mode == 'result':
            assessment['findings'][0]['quote'] = QUOTE+' mismatched decoded return'
            output = {'result_json': json.dumps(assessment)} if request['payload']['result_codec'] == 'json_string_v1' else assessment
        return output

    monkeypatch.setattr(local_executor_client, 'await_request', complete)
    claim = runtime.advance(ref)['claim']
    result = await runtime.execute(claim)
    return ref, claim, result, captured, retained


@pytest.mark.parametrize('reuse,read_tool,mode', [(False, 'lead_public_read','valid'), (True, 'lead_public_read','valid'),
    (True, 'lead_document_read','valid'), (False, 'lead_public_read','redirect')])
async def test_actual_native_read_adds_new_or_reused_source_to_complete_research(growth_agent, monkeypatch, reuse, read_tool, mode):
    ref, claim, result, captured, retained = await native_research(growth_agent, monkeypatch, reuse=reuse, read_tool=read_tool, mode=mode)
    assert result['outcome'] == 'completed', (result['outcome'], result['failure_code'])
    identifiers = {str(item['id']) for item in result['evidence_refs']}
    assert captured['document_id'] in identifiers and len(identifiers) == 2
    assert any(page['document_id'] == captured['document_id'] and page['url'] == NEW['url'] for page in result['output']['pages'])
    assert result['output']['findings'][0]['document_id'] == captured['document_id']
    assert result['output']['learning_progress'] is True
    assert runtime.finish(claim, result)['accepted_control']
    if not reuse and mode == 'valid':
        with tx(growth_agent) as c:
            experiment = one(c, 'SELECT experiment_id FROM lead.tasks WHERE id=%s', (ref['task_id'],))['experiment_id']
            spec = TaskSpec(kind='content', purpose='use-actual-criteria', experiment_id=experiment,
                evidence_refs=result['evidence_refs'], payload={'brief': 'Explain the retained source criteria.'},
                review_at=utcnow()+timedelta(hours=1))
            content = submit_work(c, growth_agent, spec)

        async def draft(schema, context, **kwargs):
            assert schema is tools.ContentDraft
            assert any(str(source['id']) == captured['document_id'] and QUOTE in source['body'] for source in context['sources'])
            return schema(body='The source describes transparent ownership.', claim_checks=[{
                'claim': 'The source describes transparent ownership.',
                'source': {'kind': 'document', 'id': captured['document_id'], 'revision': 1}, 'basis': 'observed'}])

        monkeypatch.setattr(tools, 'run_model', draft)
        output = await runtime.execute(runtime.advance(content)['claim'])
        assert output['outcome'] == 'completed', output
        with tx(growth_agent) as c:
            saved = one(c, 'SELECT source_refs FROM lead.content_items WHERE id=%s', (output['artifacts'][0]['id'],))
        assert any(source['id'] == captured['document_id'] for source in saved['source_refs'])
    if retained:
        assert str(retained.id) == captured['document_id']
    with tx(growth_agent) as c:
        assert one(c, 'SELECT count(*) n FROM lead.local_executor_requests')['n'] == 1
        assert one(c, "SELECT count(*) n FROM lead.local_executor_commands WHERE kind='run'")['n'] == 1


@pytest.mark.parametrize('mode', ['unread','thread','turn','failed','error','forged_body','server','request','task',
                                 'body_hash','revision','agent','tenant','generation','quote','result',
                                 'configuration','control','goal','paused'])
async def test_unproven_native_citation_stays_rejected(growth_agent, monkeypatch, mode):
    _, _, result, _, _ = await native_research(growth_agent, monkeypatch, mode=mode)
    assert result['outcome'] == 'failed' and result['failure_code'] == 'unsupported_research_finding', result


async def test_reread_keeps_old_document_times_and_original_finding_deduplication(growth_agent, monkeypatch):
    _, claim, first, _, _ = await native_research(growth_agent, monkeypatch)
    assert first['outcome'] == 'completed' and first['output']['learning_progress']
    assert runtime.finish(claim, first)['accepted_control']
    with tx(growth_agent) as c:
        before = c.execute('SELECT id,created_at,sha256 FROM lead.knowledge_documents ORDER BY id').fetchall()
    _, _, repeated, _, _ = await native_research(growth_agent, monkeypatch, reuse=True, dedupe='same-question-repeat')
    assert repeated['outcome'] == 'completed'
    assert not repeated['output']['knowledge_progress']
    assert not repeated['output']['learning_progress'] and not repeated['output']['business_progress']
    assert repeated['output']['finding_hash'] == first['output']['finding_hash']
    with tx(growth_agent) as c:
        assert c.execute('SELECT id,created_at,sha256 FROM lead.knowledge_documents ORDER BY id').fetchall() == before
