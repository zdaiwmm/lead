"""Actual A SDK receives the desired operation and repairs a wrong proposal once."""
import json
import os

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_reply_discovery_rejects_publish_then_records_only_reply(monkeypatch):
    from lead_app import account_profiles
    from lead_app.account_connection import execute_account
    from lead_app.config import STATE
    from lead_app.db import one, tx
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import accept_commands, objective_revision
    from lead_app.growth.tools import AccountInput
    from lead_app.native_task import NativeTask
    from native_connection_product import verify_login

    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def reply(context):
        aid=context['aid']
        board=context['board']
        board.state.posts.append({'id':1,'author':'another-member','body':'Existing ordinary member question','parent_url':None})
        parent=board.origin+'/posts/1'
        target=context['discovery'].output['targets'][0]['target_id']
        assert context['discovery'].output['targets'][0]['operation']=='publish'
        with tx(aid) as c:
            old=one(c,'SELECT verified_permissions FROM lead.account_targets WHERE id=%s',(target,))['verified_permissions']['browser_methods']['publish']
            revision=objective_revision(one(c,'SELECT * FROM lead.agents WHERE id=%s',(aid,)))
        proposed=accept_commands(aid,{'context_key':'desired-reply-fixture','rationale':'Discover this original reply operation',
            'commands':[{'command_key':'desired-reply','group_key':'desired-reply','kind':'target.propose',
                'account_id':context['pending']['account_id'],'goal_ref':{'revision':revision},
                'payload':{'target_urls':[parent],'desired_operation':'reply'}}]})
        assert not proposed['rejected'] and len(proposed['accepted'])==1
        ref=proposed['accepted'][0]['task_refs'][0]
        admitted=advance(ref)
        assert admitted['state']=='ready'
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert task['input']['spec']['payload']['desired_operation']=='reply'
        rejected=[]
        original=NativeTask.record_method
        async def record(self,args):
            try:
                return await original(self,args)
            except ValueError as error:
                if getattr(error,'code',None)=='method_discovery_operation_mismatch':
                    rejected.append({'operation':args['operation'],'observations':len(self.observations)})
                raise
        monkeypatch.setattr(NativeTask,'record_method',record)
        before=len(context['model_calls'])
        context['workflow'].update(kind='discovery',step=0,operation='reply',desired_operation='reply',editor_url=parent)
        result=await execute_account(aid,task,AccountInput.model_validate(task['input']['spec']['payload']))
        finish(admitted['claim'],result)
        with tx(aid) as c:
            methods=one(c,'SELECT verified_permissions FROM lead.account_targets WHERE id=%s',(target,))['verified_permissions']['browser_methods']
            finished=one(c,'SELECT state,result FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        rows=context['model_calls'][before:]
        folder=next((STATE/'browser-use-runs').glob('task-'+str(ref['task_id'])+'-*'))
        trace=json.loads((folder/'safe-step-trace.json').read_text())
        worker=json.loads((folder/'result.json').read_text())
        proof={'requests':len(rows),'task_state':finished['state'],'output_operations':[row['operation'] for row in result.output.get('targets',[])],
            'desired_reply_context_seen':[row['desired_reply_context_seen'] for row in rows],
            'desired_reply_goal_seen':[row['desired_reply_goal_seen'] for row in rows],
            'feedback_seen':[row['operation_feedback_seen'] for row in rows],
            'rejected':rejected,'corrections':finished['result']['runtime']['native_progress']['corrections'],
            'publish_method_unchanged':methods['publish']==old,'reply_method_stored':methods['reply']['operations']==['reply'],
            'worker_success':worker['success'],'trace':trace,'usage':worker['usage'],
            'credential_literal_seen':any(row['username_literal_seen'] or row['password_literal_seen'] for row in rows),
            'posts':len([row for row in board.oracle()['requests'] if row['method']=='POST' and row['path'] in ('/posts','/replies')])}
        (STATE/'discovery-operation-product.json').write_text(json.dumps(proof,indent=2))
        assert result.outcome=='completed' and proof['output_operations']==['reply']
        assert proof['requests']==4 and all(proof['desired_reply_context_seen']) and all(proof['desired_reply_goal_seen'])
        assert proof['feedback_seen'][2] and proof['rejected']==[{'operation':'publish','observations':0}]
        assert proof['corrections']==1 and proof['publish_method_unchanged'] and proof['reply_method_stored']
        assert trace['steps'][1]['outcomes']==[{'status':'error','code':'method_discovery_operation_mismatch'}]
        assert proof['worker_success'] and proof['task_state']=='succeeded' and not proof['posts'] and not proof['credential_literal_seen']
        return proof
    result=await verify_login(monkeypatch,mode='operation_feedback',after_discovery=reply)
    assert result['action_check'] is not None and result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}
