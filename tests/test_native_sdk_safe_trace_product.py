"""Same actual SDK loop: bad index, denied input, then normal A-only recovery."""
import json
import os

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_sdk_trace_distinguishes_bad_index_and_input_denial_without_values(monkeypatch):
    from lead_app import account_profiles
    from lead_app.config import STATE
    from native_connection_product import verify_login

    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    result=await verify_login(monkeypatch,mode='trace_probe')
    assert result['task_state']=='succeeded' and result['account']['state']=='connected'
    folder=STATE/'browser-use-runs'/('connection-'+result['connection_id'])
    path=folder/'safe-step-trace.json'
    assert path.is_file(), 'Actual SDK execution must persist a safe action trace.'
    raw=path.read_text()
    trace=json.loads(raw)
    assert trace['steps'][0]['actions']==[{'name':'guarded_input','index':999999}]
    assert trace['steps'][0]['outcomes']==[{'status':'error','code':'element_no_longer_observed'}]
    assert trace['steps'][1]['outcomes']==[{'status':'denied','code':'credential_placeholder_denied'}]
    assert trace['steps'][2]['outcomes']==[{'status':'ok'}]
    assert trace['steps'][-1]['actions']==[{'name':'report_connection'}]
    assert trace['steps'][-1]['outcomes']==[{'status':'done','success':True}]
    assert len(trace['steps'])==7 and not trace['truncated']
    assert path.stat().st_mode&0o777==0o600
    assert all(value not in raw for value in ('unapproved-placeholder','saved_username','saved_password',result['account']['external_account_id'],'http://','self_subject_selector'))
    assert result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}
    assert not any(row['trace_in_model_prompt'] for row in result['synthetic_model_diagnostics'])
    assert result['usage']['request_count']==7 and not result['usage']['billing_unknown']
    assert len([row for row in result['oracle']['requests'] if row['method']=='POST' and row['path']=='/session'])==1
    (STATE/'safe-trace-product.json').write_text(json.dumps({'connection_id':result['connection_id'],'trace':trace,'privacy':result['privacy'],
        'task_state':result['task_state'],'account_state':result['account']['state'],'usage':result['usage']},indent=2))
