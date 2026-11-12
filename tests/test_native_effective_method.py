"""One original attempt's technical method revision and receipt projections."""
import json
from datetime import timedelta

from lead_app.browser_actions import (
    claim_request,
    execution_action,
    method_hash,
    prepare_binding,
)
from lead_app.browser_capabilities import verified_operations
from lead_app.db import j, one, tx
from lead_app.growth.contracts import Wait, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from test_native_method_recovery import correct_current_method, prepare_original


def repaired_action(aid):
    account, observed, ref, _, _, action, actor, claim=prepare_original(aid)
    finish(claim,WorkResult(outcome='waiting',wait=Wait(reason='method_correction',producer='channel_adapter',
        check_at=utcnow()+timedelta(minutes=10),recovery_action='recheck',episode_root=ref['task_id'],scope_ref=str(action['id']))))
    _,corrected=correct_current_method(aid,account,observed,'method_only')
    claim=advance(ref)['claim']
    prepare_binding(actor,action['id'],claim,actor.epoch)
    method=corrected['browser_method']
    assert claim_request(actor,action['id'],claim,actor.epoch,
        {'method':'POST','url':method['request']['url'],'hasPostData':True,'postData':json.dumps({'body':action['intent']['body']})},
        {'subject':account['external_account_id'],'target_url':method['target_url'],'body':action['intent']['body']})
    return account,action,method


def test_repaired_method_is_an_execution_projection_and_promotes_its_actual_receipt(growth_agent):
    aid=growth_agent
    account,action,method=repaired_action(aid)
    with tx(aid) as c:
        frozen=one(c,'SELECT * FROM lead.actions WHERE id=%s',(action['id'],))
        assert frozen['intent']==action['intent']
        assert execution_action(c,frozen)['intent']['browser_method']==method
        url=method['target_url']+'/actual-post'
        c.execute("UPDATE lead.action_attempts SET outcome='confirmed',evidence_ref='fixture:repaired-receipt',observation=observation || %s WHERE action_id=%s",
            (j({'browser_receipt':{'author':account['external_account_id'],'body':action['intent']['body'],'url':url}}),action['id']))
        c.execute("UPDATE lead.actions SET state='confirmed',external_id=%s,external_url=%s,evidence_ref='fixture:repaired-receipt',confirmed_at=now() WHERE id=%s",(url,url,action['id']))
        assert one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
        assert one(c,'SELECT lead.growth_confirm_publication(%s) ok',(action['id'],))['ok']
        assert not one(c,'SELECT lead.growth_confirm_publication(%s) ok',(action['id'],))['ok']
        assert verified_operations(c,account['id'])==['publish']
        attempt=one(c,'SELECT observation FROM lead.action_attempts WHERE action_id=%s',(action['id'],))['observation']
        assert attempt['method_repair']['original_method_hash']==method_hash(action['intent']['browser_method'])
        assert attempt['method_repair']['original_method']==action['intent']['browser_method']
        assert attempt['effective_method']==method
        assert one(c,'SELECT intent FROM lead.actions WHERE id=%s',(action['id'],))['intent']==action['intent']
        # This confirmed fact is still about its original subject after rebind;
        # it cannot promote an operation for that new binding.
        c.execute("UPDATE lead.social_accounts SET external_account_id='replacement-subject',binding_version=binding_version+1 WHERE id=%s",(account['id'],))
        assert one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
        assert not one(c,'SELECT lead.growth_confirm_publication(%s) ok',(action['id'],))['ok']
        assert verified_operations(c,account['id'])==[]
        c.execute("UPDATE lead.action_attempts SET observation=jsonb_set(observation,'{method_repair,original_method}',%s) WHERE action_id=%s",(j(method),action['id']))
        assert not one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
