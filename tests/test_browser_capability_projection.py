"""Readiness needs the current original action's receipt, binding and method."""
import copy

import pytest
from lead_app import domain
from lead_app.browser_actions import claim_request, method_hash, prepare_binding
from lead_app.browser_capabilities import verified_operations
from lead_app.db import j, one, tx
from test_browser_action_transactions import native_work


@pytest.mark.parametrize("change",["method","binding","grant","hash"])
def test_native_readiness_does_not_inherit_a_method_claim_old_binding_or_revoked_grant(growth_agent,change):
    actor,action,claim,request,observed,social=native_work(growth_agent)
    prepare_binding(actor,action['id'],claim,1)
    assert claim_request(actor,action['id'],claim,1,request,observed)
    method=copy.deepcopy(action['intent']['browser_method'])
    marker={'publish':{'action_id':str(action['id']),'binding_version':social['binding_version'],
        'method_hash':method_hash(method),'method_version':'native-browser-v1'}}
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s',
            (j({'browser_methods':{'publish':method},'browser_verified_operations':marker}),action['target_id']))
        assert verified_operations(c,social['id'])==[]
        c.execute("""INSERT INTO lead.lab_posts(tenant_id,agent_id,operation_key,body)
            VALUES(lead.ctx_tenant(),%s,%s,%s)""",(growth_agent,action['operation_key'],action['intent']['body']))
        url='https://fixture.test/posts/receipt'
        c.execute("UPDATE lead.action_attempts SET outcome='confirmed',observation=observation || %s WHERE action_id=%s",
            (j({'browser_receipt':{'author':social['external_account_id'],'body':action['intent']['body'],'url':url}}),action['id']))
        c.execute("""UPDATE lead.actions SET state='confirmed',external_id='fixture-post',external_url=%s,
            evidence_ref='synthetic:original-attempt',confirmed_at=now() WHERE id=%s""",(url,action['id']))
        assert verified_operations(c,social['id'])==['publish']
    current=next(a for a in domain.agent_detail(growth_agent)['accounts'] if a['id']==social['id'])
    assert current['verified_operations']==['publish'] and current['operational_ready']
    with tx(growth_agent) as c:
        if change=="method":
            changed=copy.deepcopy(method)
            changed['request']['url']+='/new-handler'
            c.execute("UPDATE lead.account_targets SET verified_permissions=jsonb_set(verified_permissions,'{browser_methods,publish}',%s) WHERE id=%s",(j(changed),action['target_id']))
        elif change=="binding":
            c.execute('UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s',(social['id'],))
        elif change=="grant":
            c.execute('UPDATE lead.authorization_grants SET revoked_at=now() WHERE id=%s',(action['grant_id'],))
        else:
            c.execute("UPDATE lead.account_targets SET verified_permissions=jsonb_set(verified_permissions,'{browser_verified_operations,publish,method_hash}',%s) WHERE id=%s",(j('unproved-method'),action['target_id']))
        assert verified_operations(c,social['id'])==[]
    with tx(growth_agent) as c:
        assert one(c,'SELECT state FROM lead.actions WHERE id=%s',(action['id'],))['state']=='confirmed'
