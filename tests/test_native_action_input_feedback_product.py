"""A-only actual SDK input, current editor feedback, submit and canonical receipt."""
import json
import os
import shlex
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_authorized_body_input_has_current_match_feedback_before_submit(monkeypatch):
    from lead_app import account_profiles
    from lead_app.config import STATE
    from lead_app.db import j, one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel
    from lead_app.native_action import NativeAction
    from native_connection_product import verify_login

    python=os.environ['LEAD_BROWSER_RUNTIME_PYTHON']
    bootstrap=STATE/'input-feedback-bootstrap.py'
    interpreter=STATE/'input-feedback-python'
    def transport(loopback):
        bootstrap.write_text('''import runpy,sys,httpx
real=httpx.AsyncHTTPTransport
class LocalOnly(httpx.AsyncBaseTransport):
 async def handle_async_request(self,request):
  if request.url.scheme=='http' and request.url.host=='127.0.0.1':
   async with httpx.AsyncClient(transport=real(trust_env=False),trust_env=False,timeout=45) as client:
    return await client.send(request)
  assert request.url.scheme=='https' and request.url.host=='api.deepseek.com'
  async with httpx.AsyncClient(transport=real(trust_env=False),trust_env=False,timeout=45) as client:
   response=await client.post(LOOPBACK,content=request.content,headers={'Content-Type':'application/json'})
   return httpx.Response(response.status_code,headers=response.headers,content=response.content,request=request)
httpx.AsyncHTTPTransport=lambda **kwargs:LocalOnly()
path=next(value for value in sys.argv[1:] if value not in ('-B','-S'))
runpy.run_path(path,run_name='__main__')
'''.replace('LOOPBACK',repr(loopback)))
        bootstrap.chmod(0o600)
        interpreter.write_text('#!/bin/sh\nexec '+shlex.quote(python)+' -B -S '+shlex.quote(str(bootstrap))+' "$@"\n')
        interpreter.chmod(0o700)
        monkeypatch.setenv('LEAD_BROWSER_RUNTIME_PYTHON',str(interpreter))
        return {'provider':'deepseek','key':'synthetic-transport-only','model':'deepseek-flash','base_url':'https://api.deepseek.com'}

    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def publish(context):
        aid=context['aid']
        body={'body':'Controlled authorized editor body canary','title':'','format':'text','media':[]}
        with tx(aid) as c:
            content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(aid,j(body),digest(body)))
            data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'])
            target=context['discovery'].output['targets'][0]['target_id']
            ref=submit_work(c,aid,TaskSpec(kind='channel',purpose='publish',account_id=context['pending']['account_id'],
                target_id=target,payload=data.model_dump(mode='json'),review_at=utcnow()+timedelta(hours=1)))
        claim=advance(ref)['claim']
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        context['workflow'].update(kind='action',step=0,body=body['body'],operation='publish',input_feedback_probe=True)
        original=NativeAction.descriptor
        async def descriptor(self):
            result=await original(self)
            if context['workflow']['step']<=1:
                editor=self.page.locator(self.action['intent']['browser_method']['selectors']['editor_body'])
                context['workflow']['editor_box']=await editor.bounding_box()
                context['workflow']['editor_value_matches_authorized']=(await editor.input_value())==body['body']
            return result
        monkeypatch.setattr(NativeAction,'descriptor',descriptor)
        before=len(context['model_calls'])
        result=await execute_channel(aid,task,data,claim)
        finish(claim,result)
        rows=context['model_calls'][before:]
        with tx(aid) as c:
            action=one(c,'SELECT * FROM lead.actions WHERE task_id=%s',(task['id'],))
            attempt=one(c,'SELECT * FROM lead.action_attempts WHERE action_id=%s',(action['id'],))
            receipt_valid=one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
        proof={'requests':len(rows),'action_state':action['state'],'attempt':attempt['attempt'],
            'receipt_valid':receipt_valid,'body_before_input':rows[0]['editor_value_matches_authorized'],
            'body_after_input':rows[1]['editor_value_matches_authorized'],
            'body_visible_in_next_dom':rows[1]['textarea_value_visible_in_dom'],
            'body_visible_in_next_pixels':rows[1]['editor_dark_pixels']>rows[0]['editor_dark_pixels']+50,
            'native_typed_text_echo_seen':rows[1]['native_typed_text_echo_seen'],
            'explicit_body_confirmation_seen':rows[1]['body_confirmation_seen'],
            'images':[row['image_parts'] for row in rows],
            'credential_literals_seen':any(row['username_literal_seen'] or row['password_literal_seen'] for row in rows),
            'posts':len([row for row in context['board'].oracle()['requests'] if row['method']=='POST' and row['path']=='/posts'])}
        (STATE/'input-feedback-product.json').write_text(json.dumps(proof,indent=2))
        assert result.outcome=='completed' and proof['receipt_valid'] and action['state']=='confirmed'
        assert proof['requests']==3 and proof['attempt']==1 and proof['posts']==1
        assert proof['body_before_input'] is False and proof['body_after_input'] is True
        assert proof['body_visible_in_next_pixels'] and proof['images']==[1,1,1]
        assert not proof['credential_literals_seen']
        assert proof['explicit_body_confirmation_seen']
        return proof
    result=await verify_login(monkeypatch,model_transport=transport,after_discovery=publish)
    assert result['action_check'] is not None
    assert result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}
