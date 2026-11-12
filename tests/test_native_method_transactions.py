"""Original target/grant and first-receipt promotion, with actual scoped SQL."""
import copy

import pytest
from lead_app.account_connection import derive_target
from lead_app.account_intents import set_intent
from lead_app.browser_actions import claim_request, method_hash, prepare_binding
from lead_app.db import j, one, tx
from test_browser_action_transactions import native_work


def observed_method(aid):
    from datetime import timedelta

    from lead_app.execution_routes import resolve
    from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import submit_work
    _,action,claim,_,_,account=native_work(aid)
    finish(claim,WorkResult(outcome='failed',failure_code='SYNTHETIC_SETUP'))
    set_intent(aid,account['id'],'running',source='owner_command',source_ref='native-member-scope',
        scope={'target_scope':'authenticated_permitted_targets','excluded_targets':[]})
    method=copy.deepcopy(action['intent']['browser_method'])
    method['receipt_state']='unobserved'
    method['selectors']={key:value for key,value in method['selectors'].items() if not key.startswith('receipt_')}
    observed={'external_target_id':method['target_url'],'canonical_url':method['target_url'],'display_name':'Synthetic member board',
        'acting_subject':account['external_account_id'],'access_mode':'ordinary_member','evidence_refs':['fixture:current-dom'],
        'browser_method':method}
    with tx(aid) as c:
        work=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=account['id'],review_at=utcnow()+timedelta(hours=1)))
    claim=advance(work)['claim']
    with tx(aid) as c:
        intent=one(c,'SELECT revision FROM lead.account_operating_intents WHERE account_id=%s',(account['id'],))
    observed['producer_scope']={'task_id':str(work['task_id']),'task_generation':work['task_generation'],
        'attempt_token':claim['attempt_token'],'control_version':claim['control_version'],'intent_revision':intent['revision'],
        'execution_ref':resolve(aid,profile_id=account['profile_id']).model_dump(mode='json')}
    return account,observed


def test_operation_methods_preserve_publish_when_independent_reply_is_observed(growth_agent):
    account,observed=observed_method(growth_agent)
    first=derive_target(growth_agent,account,observed)
    assert first['knowledge_progress']
    repeated=copy.deepcopy(observed)
    repeated['browser_method']['evidence_refs']=['fixture:fresh-but-identical-dom']
    assert not derive_target(growth_agent,account,repeated)['knowledge_progress']
    reply=copy.deepcopy(observed)
    reply['browser_method']['operations']=['reply']
    reply['browser_method']['selectors']['editor_parent']='#parent'
    reply['browser_method']['request'].update(url=observed['canonical_url']+'/replies',parent_field='parent')
    second=derive_target(growth_agent,account,reply)
    assert first['target_id']==second['target_id'] and second['knowledge_progress']
    with tx(growth_agent) as c:
        target=one(c,'SELECT * FROM lead.account_targets WHERE id=%s',(first['target_id'],))
        assert target['verified_permissions']['browser_methods']['publish']==observed['browser_method']
        assert target['verified_permissions']['browser_methods']['reply']==reply['browser_method']
        assert one(c,'SELECT allowed_actions FROM lead.authorization_grants WHERE id=%s',(second['grant_id'],))['allowed_actions']==['publish','reply']
        assert one(c,'SELECT revoked_at FROM lead.authorization_grants WHERE id=%s',(first['grant_id'],))['revoked_at']


def test_all_observed_targets_share_one_original_claim_transaction(growth_agent):
    from lead_app.account_connection import derive_targets
    account,first=observed_method(growth_agent)
    stale=copy.deepcopy(first)
    stale['external_target_id']=stale['canonical_url']+'/second'
    stale['canonical_url']=stale['external_target_id']
    stale['browser_method']['target_url']=stale['canonical_url']
    stale['producer_scope']['attempt_token']='old-worker'
    with pytest.raises(ValueError,match='native_method_observation_expired'):
        derive_targets(growth_agent,account,[first,stale])
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.account_targets WHERE account_id=%s AND target_type='web'",(account['id'],))['n']==0


@pytest.mark.parametrize('change',['subject','binding','pause','pause_resume','control_resume','old_claim','old_owned_scope','malformed_permissions'])
def test_native_method_cannot_create_permission_from_stale_or_unrelated_scope(growth_agent,change):
    account,observed=observed_method(growth_agent)
    if change=='subject':
        observed['acting_subject']='different-subject'
    elif change=='binding':
        observed['browser_method']['binding_version']+=1
    elif change=='pause':
        set_intent(growth_agent,account['id'],'paused',source='owner_command',source_ref='native-pause')
    elif change=='pause_resume':
        set_intent(growth_agent,account['id'],'paused',source='owner_command',source_ref='native-pause')
        set_intent(growth_agent,account['id'],'running',source='owner_command',source_ref='native-resume')
    elif change=='control_resume':
        from lead_app import domain
        domain.set_control(growth_agent,'pause')
        domain.set_control(growth_agent,'resume')
    elif change=='old_claim':
        observed['producer_scope']['attempt_token']='old-worker-claim'
    elif change=='old_owned_scope':
        set_intent(growth_agent,account['id'],'running',source='owner_command',source_ref='native-owned-only',
            scope={'target_scope':'verified_owned_targets'})
    else:
        value=derive_target(growth_agent,account,observed)
        with tx(growth_agent) as c:
            c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s',(j([]),value['target_id']))
    with pytest.raises(ValueError):
        derive_target(growth_agent,account,observed)


def test_confirmed_original_receipt_promotes_current_method_only_once(growth_agent):
    actor,action,claim,request,observed,account=native_work(growth_agent)
    prepare_binding(actor,action['id'],claim,1)
    assert claim_request(actor,action['id'],claim,1,request,observed)
    method=action['intent']['browser_method']
    url='https://fixture.test/posts/actual-receipt'
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s',
            (j({'browser_methods':{'publish':method}}),action['target_id']))
        c.execute('INSERT INTO lead.lab_posts(tenant_id,agent_id,operation_key,body) VALUES(lead.ctx_tenant(),%s,%s,%s)',
            (growth_agent,action['operation_key'],action['intent']['body']))
        c.execute("UPDATE lead.action_attempts SET outcome='confirmed',observation=observation || %s WHERE action_id=%s",
            (j({'browser_receipt':{'author':account['external_account_id'],'body':action['intent']['body'],'url':url}}),action['id']))
        c.execute("UPDATE lead.actions SET state='confirmed',external_id='fixture-post',external_url=%s,evidence_ref='fixture:actual-receipt',confirmed_at=now() WHERE id=%s",(url,action['id']))
        assert one(c,'SELECT lead.growth_confirm_publication(%s) promoted',(action['id'],))['promoted']
        assert not one(c,'SELECT lead.growth_confirm_publication(%s) promoted',(action['id'],))['promoted']
        marker=one(c,'SELECT verified_permissions FROM lead.account_targets WHERE id=%s',(action['target_id'],))['verified_permissions']['browser_verified_operations']['publish']
        assert marker['action_id']==str(action['id']) and marker['method_hash']==method_hash(method)
