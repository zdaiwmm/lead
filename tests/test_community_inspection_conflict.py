"""A rejected group inspection is not proof that every group is unreadable."""
import json
from datetime import timedelta

import httpx
import pytest
from lead_app import activities, browser
from lead_app.db import j, one, tx
from lead_app.growth import agent, runtime
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.store import submit_work
from lead_app.scope import service_scope
from test_community_read_proof import observed as observed
from test_community_read_proof import produce
from test_community_read_recovery import failed_read
from test_growth_telegram_community import PEER, URL, source, work
from test_growth_telegram_community import community as community


async def test_rejected_inspection_keeps_real_recovery_and_allows_next_independent_work(observed, monkeypatch):
    old, root, _ = failed_read(observed, 'search')
    with tx(observed.aid) as c:
        frozen = root['input']
        frozen['spec']['review_at'] = (utcnow()-timedelta(minutes=1)).isoformat()
        c.execute('UPDATE lead.tasks SET input=%s WHERE id=%s', (j(frozen), old['task_id']))
    proof = await produce(observed)
    with service_scope():
        assert runtime.consume_community_read_proof(observed.aid, proof['proof_ref'])['recovered_episodes'] == 1
    with tx(observed.aid) as c:
        before = one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (old['task_id'],))
    predecessor = work(observed, 'search')
    claim = runtime.advance(predecessor)['claim']
    groups = [{'group_id':PEER, 'canonical_url':URL, 'title':'First discussion', 'kind':'discussion',
               'subtitle':'@privacy_fixture, 20 members'},
              {'group_id':'-778890', 'canonical_url':'https://t.me/another_fixture', 'title':'Other discussion',
               'kind':'discussion', 'subtitle':'@another_fixture, 30 members'}]
    runtime.finish(claim, WorkResult(outcome='completed', output={'groups':groups}))
    rejected = work(observed, 'inspect', predecessor)
    claim = runtime.advance(rejected)['claim']
    calls = []

    async def conflict(*args, **kwargs):
        calls.append('inspect')
        raise ValueError('community_discussion_binding_required')

    original_client = httpx.AsyncClient

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.ASGITransport(app=browser.app, raise_app_exceptions=False))

    monkeypatch.setattr(browser, 'execute_community_step', conflict)
    monkeypatch.setattr(activities.httpx, 'AsyncClient', client)
    with service_scope():
        result = await runtime.execute(claim)
    assert result['failure_code'] == 'community_discussion_binding_required'
    assert result['output'] == {'http_diagnostic':{'status_code':409, 'operation':'inspect'}}
    assert 'NEVER_EXPORT' not in json.dumps(result) and 'private.invalid' not in json.dumps(result)
    runtime.finish(claim, result)
    with tx(observed.aid) as c:
        assert one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (old['task_id'],)) == before
        failed = one(c, 'SELECT state,result FROM lead.tasks WHERE id=%s', (rejected['task_id'],))
        assert failed['state'] == 'failed' and 'recovery' not in failed['result']['runtime']
        next_ref = submit_work(c, observed.aid, TaskSpec(kind='account', purpose='community', account_id=observed.account['id'],
            payload={'community_operation':'inspect', 'group_task_id':predecessor['task_id'], 'group_id':groups[1]['group_id']},
            required_refs=[{'task_id':predecessor['task_id'], 'reason':'Inspect this independently observed group.'}], review_at=utcnow()+timedelta(hours=1)))
    next_claim = runtime.advance(next_ref)['claim']
    group = {**groups[1], 'rules':'Useful privacy discussion. Be respectful.', 'rules_hash':'fixture',
        'rules_status':'complete', 'automation_forbidden':False, 'membership':'not_joined', 'can_reply':False,
        'observed_at':utcnow().isoformat(), 'messages':[]}
    runtime.finish(next_claim, WorkResult(outcome='completed', output={'group':group}))
    with tx(observed.aid) as c:
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (next_ref['task_id'],))['state'] == 'succeeded'
        assert one(c, 'SELECT count(*) n FROM lead.actions')['n'] == 0
    assert calls == ['inspect']


def test_known_broadcast_is_excluded_and_rejected_before_browser_work(community):
    predecessor = source(community, {'group_id':PEER, 'canonical_url':URL, 'title':'Broadcast fixture',
        'subtitle':'@privacy_fixture, 20 subscribers'})
    assert agent.build_context(community.aid)['community_candidates']['items'] == []
    with tx(community.aid) as c:
        before = one(c, 'SELECT count(*) n FROM lead.tasks')['n']
    with pytest.raises(ValueError, match='community_discussion_binding_required'):
        work(community, 'inspect', predecessor)
    with tx(community.aid) as c:
        assert one(c, 'SELECT count(*) n FROM lead.tasks')['n'] == before
        assert one(c, 'SELECT count(*) n FROM lead.actions')['n'] == 0


@pytest.mark.parametrize('payload', [None, [], {'code':[]}, {'code':True},
    {'code':'community_unknown_NEVER_EXPORT'}, {'code':'Telegram 登录已失效，请重新连接账号'}])
async def test_unrecognized_inspection_conflict_retains_original_failure_scope(community, monkeypatch, payload):
    ref = work(community, 'inspect', source(community))
    claim = runtime.advance(ref)['claim']

    async def conflict(*args, **kwargs):
        httpx.Response(409, request=httpx.Request('POST', 'https://private.invalid/secret'),
            json=payload).raise_for_status()

    monkeypatch.setattr(activities, 'browser_rpc', conflict)
    with service_scope():
        result = await runtime.execute(claim)
    assert result['failure_code'] == 'HTTPStatusError'
    assert result['output'] == {'http_diagnostic':{'status_code':409, 'operation':'inspect'}}
    runtime.finish(claim, result)
    with tx(community.aid) as c:
        episode = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (ref['task_id'],))['result']['runtime']['recovery']
        assert episode['resource'] == 'profile:'+str(community.account['profile_id'])
        assert episode['error_code'] == 'HTTPStatusError' and episode['attempts'] == 1
    assert 'NEVER_EXPORT' not in json.dumps(result) and 'private.invalid' not in json.dumps(result)


@pytest.mark.parametrize('handle', ['subscribers', 'subscriber'])
def test_account_handle_is_not_a_broadcast_label(community, handle):
    predecessor = source(community, {'group_id':PEER, 'canonical_url':'https://t.me/'+handle,
        'title':'Discussion fixture', 'subtitle':'@'+handle+', 20 members'})
    assert len(agent.build_context(community.aid)['community_candidates']['items']) == 1
    assert work(community, 'inspect', predecessor)['task_id']
