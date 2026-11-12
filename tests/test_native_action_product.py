"""Real original native action through manager, SDK, proxy and scoped SQL."""
import os
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_native_first_publication_uses_actual_dom_receipt(monkeypatch):
    from lead_app import account_profiles
    from lead_app.db import j, one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel
    from native_connection_product import verify_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def publish(context):
        aid=context['aid']
        body={'body':'Controlled first native publication','title':'','format':'text','media':[]}
        with tx(aid) as c:
            content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(aid,j(body),digest(body)))
            data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'])
            target=context['discovery'].output['targets'][0]['target_id']
            ref=submit_work(c,aid,TaskSpec(kind='channel',purpose='publish',account_id=context['pending']['account_id'],target_id=target,
                payload=data.model_dump(mode='json'),review_at=utcnow()+timedelta(hours=1)))
        claim=advance(ref)['claim']
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        context['workflow'].update(kind='action',step=0,body=body['body'],operation='publish')
        result=await execute_channel(aid,task,data,claim)
        finish(claim,result)
        with tx(aid) as c:
            action=one(c,'SELECT * FROM lead.actions WHERE task_id=%s',(task['id'],))
            attempt=one(c,'SELECT * FROM lead.action_attempts WHERE action_id=%s',(action['id'],))
            print('ACTION_DIAGNOSTICS',{'state':action['state'],'transport':attempt['observation'].get('browser_transport'),
                'claimed':attempt['observation'].get('browser_request',{}).get('claimed'),
                'fixture_posts':len(context['board'].oracle()['posts']),'fake_requests':context['workflow']['step']})
            assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(task['id'],))['state']=='succeeded'
            assert one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
        assert result.outcome=='completed' and result.output['first_format_verified']
        assert action['state']=='confirmed' and attempt['outcome']=='confirmed'
        assert attempt['observation']['browser_receipt']['body']==body['body']
        assert len(context['board'].oracle()['posts'])==1
        assert len([row for row in context['board'].oracle()['requests'] if row['method']=='POST' and row['path']=='/posts'])==1
        return {'state':'confirmed','action_id':str(action['id']),'requests':context['workflow']['step']}
    result=await verify_login(monkeypatch,after_discovery=publish)
    assert result['action_check']['state']=='confirmed' and result['action_check']['requests']==3
