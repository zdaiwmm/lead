"""Channel identity includes its primitive and the original reply parent."""
import copy
from datetime import timedelta

import pytest
from lead_app.db import j, one, tx
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from lead_app.growth.store import digest, submit_work
from lead_app.growth.tools import ChannelInput, prepare_channel
from test_browser_action_transactions import native_work
from test_growth_channel_operation import channel_work

PARENT_ONE='https://fixture.test/posts/one'
PARENT_TWO='https://fixture.test/posts/two'


def submit_variant(aid,ref,operation,parent=None,*,omit_operation=False):
    with tx(aid) as c:
        row=one(c,'SELECT input FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        spec=TaskSpec.model_validate(row['input']['spec'])
        payload={**spec.payload,'operation':operation,'parent_url':parent}
        if omit_operation:
            payload.pop('operation')
        variant=spec.model_copy(update={'payload':payload,'purpose':'renamed-intention',
            'dedupe_key':'different-client-key','review_at':utcnow()+timedelta(hours=2)})
        return submit_work(c,aid,variant),ChannelInput.model_validate(payload)


def mark_unknown(aid,ref,claim,action):
    with tx(aid) as c:
        c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s",(action['id'],))
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s",(action['id'],))
    assert finish(claim,WorkResult(outcome='unknown'))['accepted_control']


def test_planned_publish_and_each_reply_parent_have_distinct_intentions(growth_agent):
    aid=growth_agent
    publish,_,_=channel_work(aid)
    first,_=submit_variant(aid,publish,'reply',PARENT_ONE)
    second,_=submit_variant(aid,publish,'reply',PARENT_TWO)
    assert len({publish['task_id'],first['task_id'],second['task_id']})==3
    assert submit_variant(aid,publish,'publish')[0]==publish
    assert submit_variant(aid,publish,'publish',omit_operation=True)[0]==publish
    # A top-level publication never consumes a parent; this metadata cannot
    # rename the same send into a second intention.
    assert submit_variant(aid,publish,'publish',PARENT_ONE)[0]==publish
    assert submit_variant(aid,publish,'reply',PARENT_ONE)[0]==first
    assert submit_variant(aid,publish,'reply',PARENT_TWO)[0]==second


def test_unknown_publish_and_reply_actions_deduplicate_only_the_same_primitive(growth_agent):
    aid=growth_agent
    _,publication,claim,_,_,_=native_work(aid)
    publish={'agent_id':aid,'task_id':str(publication['task_id']),'task_generation':1}
    method=publication['intent']['browser_method']
    reply=copy.deepcopy(method)
    reply['operations']=['reply']
    reply['selectors'].update(editor_parent='#parent',receipt_parent='#receipt-parent')
    reply['request'].update(url=method['target_url']+'/replies',parent_field='parent')
    with tx(aid) as c:
        c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s',
            (j({'browser_methods':{'publish':method,'reply':reply}}),publication['target_id']))
        c.execute("UPDATE lead.authorization_grants SET allowed_actions='[\"publish\",\"reply\"]' WHERE target_id=%s AND revoked_at IS NULL",
            (publication['target_id'],))
        grant_count=one(c,'SELECT count(*) n FROM lead.authorization_grants')['n']
    mark_unknown(aid,publish,claim,publication)
    first,first_data=submit_variant(aid,publish,'reply',PARENT_ONE)
    assert first['task_id']!=publish['task_id']
    first_claim=advance(first)['claim']
    first_action=prepare_channel(aid,first['task_id'],first_data)
    assert first_action['type']=='reply' and first_action['intent']['parent_url']==PARENT_ONE
    mark_unknown(aid,first,first_claim,first_action)
    second,second_data=submit_variant(aid,publish,'reply',PARENT_TWO)
    assert second['task_id'] not in (publish['task_id'],first['task_id'])
    assert advance(second)['state']=='ready'
    second_action=prepare_channel(aid,second['task_id'],second_data)
    assert second_action['type']=='reply' and second_action['intent']['parent_url']==PARENT_TWO
    assert submit_variant(aid,publish,'publish')[0]==publish
    assert submit_variant(aid,publish,'reply',PARENT_ONE)[0]==first
    assert submit_variant(aid,publish,'reply',PARENT_TWO)[0]==second
    with tx(aid) as c:
        assert one(c,'SELECT count(*) n FROM lead.actions')['n']==3
        assert one(c,'SELECT count(*) n FROM lead.authorization_grants')['n']==grant_count
        actual=one(c,'SELECT state,intent FROM lead.actions WHERE id=%s',(publication['id'],))
    assert actual['state']=='unknown' and actual['intent']==publication['intent']


@pytest.mark.parametrize('missing',['grant','reply_method'])
def test_new_reply_intention_still_requires_original_grant_and_method(growth_agent,missing):
    aid=growth_agent
    _,publication,claim,_,_,_=native_work(aid)
    publish={'agent_id':aid,'task_id':str(publication['task_id']),'task_generation':1}
    if missing=='reply_method':
        with tx(aid) as c:
            c.execute("UPDATE lead.authorization_grants SET allowed_actions='[\"publish\",\"reply\"]' WHERE target_id=%s AND revoked_at IS NULL",
                (publication['target_id'],))
    mark_unknown(aid,publish,claim,publication)
    reply,data=submit_variant(aid,publish,'reply',PARENT_ONE)
    assert reply['task_id']!=publish['task_id']
    assert advance(reply)['state']=='ready'
    with pytest.raises(ValueError,match='target_not_permitted|browser_method_(contract_invalid|scope_changed)'):
        prepare_channel(aid,reply['task_id'],data)
    with tx(aid) as c:
        assert one(c,'SELECT count(*) n FROM lead.actions')['n']==1


@pytest.mark.parametrize(('operation','parent'),[('publish',None),('reply',PARENT_ONE)])
def test_legacy_planned_workflow_is_reused_only_for_its_original_operation_and_parent(growth_agent,operation,parent):
    aid=growth_agent
    ref,_,_=channel_work(aid)
    with tx(aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        spec=TaskSpec.model_validate(row['input']['spec'])
        payload={**spec.payload,'operation':operation,'parent_url':parent}
        binding=one(c,"""SELECT s.binding_version,i.revision intent_revision,r.generation route_generation
            FROM lead.social_accounts s LEFT JOIN lead.account_operating_intents i ON i.account_id=s.id
            LEFT JOIN lead.profile_execution_routes r ON r.profile_id=s.profile_id WHERE s.id=%s""",(spec.account_id,))
        # Historical identity before operation/parent participated in the hash.
        legacy={'kind':'channel','account_id':str(spec.account_id),'target_id':str(spec.target_id),
            'goal_revision':row['input']['goal_revision'],'content':payload.get('content_id'),
            'content_hash':payload.get('content_hash'),'original_action_id':payload.get('original_action_id'),'binding':binding}
        c.execute('UPDATE lead.tasks SET workflow_id=%s,input=jsonb_set(input,\'{spec,payload}\',%s) WHERE id=%s',
            ('growth:'+aid+':'+digest(legacy),j(payload),ref['task_id']))
    assert submit_variant(aid,ref,operation,parent)[0]==ref
    changed_parent=PARENT_TWO if operation=='reply' else PARENT_ONE
    distinct,_=submit_variant(aid,ref,'reply',changed_parent)
    assert distinct['task_id']!=ref['task_id']
