"""Actual SDK input cannot send a draft or Enter-triggered post before submit proof."""
import os
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.parametrize('effect', ['autosave', 'enter_send'])
@pytest.mark.asyncio
async def test_editor_input_side_effect_is_refused_before_original_submit(monkeypatch, effect):
    from lead_app import account_profiles
    from lead_app.db import j, one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel
    from lead_app.native_action import NativeAction
    from native_connection_product import verify_login

    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    original_start = NativeAction.start
    original_authorize = NativeAction.authorize
    observed = []
    active = {}

    async def install_effect(action, *args, **kwargs):
        result = await original_start(action, *args, **kwargs)
        # Model-visible form/method/identity remain the real fixture. This adds
        # hostile site JavaScript only to the action's actual assigned document.
        await action.page.evaluate("""effect=>{
            const field=document.querySelector('textarea[name=body]');
            const send=async()=>{
                const url=effect==='autosave'?'/drafts':'/posts';
                const body=new URLSearchParams({body:field.value});
                try {
                    await fetch(url,{method:'POST',body});
                    window.__earlyInputSend='unexpected_response';
                } catch {window.__earlyInputSend='denied';}
            };
            if(effect==='autosave') field.addEventListener('input',send,{once:true});
            else {
                field.addEventListener('keydown',event=>{if(event.key==='Enter')send();},{once:true});
                field.addEventListener('input',()=>field.dispatchEvent(
                    new KeyboardEvent('keydown',{key:'Enter',bubbles:true})),{once:true});
            }
        }""", effect)
        return result

    async def inspect_before_submit(action, args):
        if args.get('name') == 'click':
            await action.page.wait_for_function("window.__earlyInputSend==='denied'", timeout=5000)
            board = active['board']
            assert not [row for row in board.oracle()['requests'] if row['method']=='POST'
                        and row['path'] in ('/posts', '/drafts')]
            with tx(action.actor.aid) as c:
                current = one(c, 'SELECT state FROM lead.actions WHERE id=%s', (action.action_id,))
                attempt = one(c, 'SELECT observation FROM lead.action_attempts WHERE action_id=%s', (action.action_id,))
            assert current['state'] == 'prepared'
            assert attempt['observation']['browser_request']['claimed'] is False
            observed.append(effect)
        return await original_authorize(action, args)

    monkeypatch.setattr(NativeAction, 'start', install_effect)
    monkeypatch.setattr(NativeAction, 'authorize', inspect_before_submit)

    async def publish(context):
        aid = context['aid']
        active['board'] = context['board']
        body = {'body': 'Controlled current content with '+effect, 'title': '', 'format': 'text', 'media': []}
        with tx(aid) as c:
            content = one(c, """INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""", (aid,j(body),digest(body)))
            data = ChannelInput(content_id=content['id'], content_hash=content['content_hash'])
            target = context['discovery'].output['targets'][0]['target_id']
            ref = submit_work(c, aid, TaskSpec(kind='channel', purpose='publish', account_id=context['pending']['account_id'],
                target_id=target, payload=data.model_dump(mode='json'), review_at=utcnow()+timedelta(hours=1)))
        claim = advance(ref)['claim']
        with tx(aid) as c:
            task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        context['workflow'].update(kind='action', step=0, body=body['body'], operation='publish')
        result = await execute_channel(aid, task, data, claim)
        finish(claim, result)
        assert observed == [effect]
        assert result.outcome == 'completed'
        source = context['board'].oracle()
        assert len(source['posts']) == 1 and source['posts'][0]['body'] == body['body']
        assert len([row for row in source['requests'] if row['method']=='POST' and row['path']=='/posts']) == 1
        assert not [row for row in source['requests'] if row['method']=='POST' and row['path']=='/drafts']
        with tx(aid) as c:
            action = one(c, 'SELECT * FROM lead.actions WHERE task_id=%s', (ref['task_id'],))
            attempt = one(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
            assert action['state']=='confirmed' and attempt['outcome']=='confirmed'
            assert one(c, 'SELECT lead.growth_receipt_valid(%s) ok', (action['id'],))['ok']
            assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (ref['task_id'],))['state']=='succeeded'
        return {'denied_early_send': effect, 'original_confirmed_once': True}

    result = await verify_login(monkeypatch, after_discovery=publish)
    assert result['action_check'] == {'denied_early_send': effect, 'original_confirmed_once': True}
