"""A-only SDK error context round trip; deterministic fake HTTP, never paid."""
import json
import os
import sys

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_bad_affix_feedback_reaches_actual_sdk_then_corrected_identity_binds(monkeypatch):
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
