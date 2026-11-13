"""A-only SDK error context round trip; deterministic fake HTTP, never paid."""
import json
import os
import sys

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_missing_element_feedback_reaches_actual_sdk_then_corrected_identity_binds(monkeypatch):
    from lead_app import account_profiles
    from lead_app.config import STATE
    from native_connection_product import verify_login

    assert sys.platform=='linux' and os.environ.get('LEAD_NETWORK_NAMESPACE')=='1'
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    result=await verify_login(monkeypatch,mode='identity_feedback')
    calls=result['synthetic_model_diagnostics']
    diagnostics={'task_state':result['task_state'],'account_state':result['account']['state'],
        'actions':[item['action'] for item in calls],
        'identity_code_visible_in_actual_request':any(item['identity_feedback_code_seen'] for item in calls),
        'privacy':result['privacy'],'usage':result['usage'],
        'auth_post_count':sum(row['method']=='POST' and row['path']=='/session' for row in result['oracle']['requests']),
        'private_self_observed':any(row['path']=='/account' for row in result['oracle']['requests']),
        'posts':len(result['oracle']['posts'])}
    (STATE/'identity-feedback-product.json').write_text(json.dumps(diagnostics,indent=2))
    assert calls[4]['identity_feedback_code_seen']
    assert diagnostics['actions']==['guarded_input','guarded_input','guarded_click','inspect_self','inspect_self','report_connection']
    assert diagnostics['task_state']=='succeeded' and diagnostics['account_state']=='connected'
    assert diagnostics['auth_post_count']==1 and diagnostics['private_self_observed'] and diagnostics['posts']==0
    assert result['usage']['request_count']==6 and not result['usage']['billing_unknown']
    assert result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}
    assert all(not row['username_literal_seen'] and not row['password_literal_seen'] for row in calls)
    assert result['temporary_login_cleared']


@pytest.mark.asyncio
@pytest.mark.parametrize('wrong_subject',[False,True])
async def test_server_identity_facts_bridge_split_sdk_text_without_model_affix_guess(monkeypatch,wrong_subject):
    from lead_app import account_profiles
    from lead_app.config import STATE
    from lead_app.native_connection import NativeConnection
    from native_connection_product import verify_login

    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    original=NativeConnection.inspect_self
    observed=[]
    async def inspect(controller,args):
        text=await controller.page.locator('main > p').inner_text()
        label=await controller.page.locator('a[rel~=me]').inner_text()
        observed.append({'actual_element_includes_link_label':label in text,
            'proposal_suffix_matches':text.endswith(args['self_subject_suffix'])})
        return await original(controller,args)
    monkeypatch.setattr(NativeConnection,'inspect_self',inspect)
    result=await verify_login(monkeypatch,mode='identity_inferred_wrong_subject' if wrong_subject else 'identity_inferred')
    calls=result['synthetic_model_diagnostics']
    evidence={'observed':observed,'sdk_split_identity':any(row.get('split_current_identity') for row in calls),
        'sdk_safe_lines':[line for row in calls for line in row.get('current_dom_lines',[])],
        'task_state':result['task_state'],'account_state':result['account']['state'],'privacy':result['privacy'],
        'actions':[row['action'] for row in calls],'private_self_observed':any(row['path']=='/account' for row in result['oracle']['requests'])}
    (STATE/'identity-inferred-product.json').write_text(json.dumps(evidence,indent=2))
    assert observed==[{'actual_element_includes_link_label':True,'proposal_suffix_matches':False}]
    assert evidence['sdk_split_identity']
    assert evidence['private_self_observed'] and result['oracle']['posts']==[]
    assert result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}
    if wrong_subject:
        assert result['task_state']=='waiting_user' and result['account']['state']=='pending'
        assert result['account']['external_account_id'] is None
    else:
        assert result['task_state']=='succeeded' and result['account']['state']=='connected'
