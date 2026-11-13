"""Publish and reply retain distinct methods and original frozen intent."""
import copy
from datetime import timedelta

from lead_app.db import j, one, tx
from lead_app.growth.agent import build_context
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from lead_app.growth.store import submit_work
from lead_app.growth.tools import ChannelInput, prepare_channel, submission_gate
from test_browser_action_transactions import native_work


def test_operation_method_map_preserves_publish_and_selects_exact_reply(growth_agent):
    actor,publish,claim,_,_,social=native_work(growth_agent)
    original=copy.deepcopy(publish['intent']['browser_method'])
    reply=copy.deepcopy(original)
    reply['operations']=['reply']
    reply['request'].update(url=original['request']['url']+'/reply',parent_field='parent_url')
    reply['selectors'].update(editor_parent='#editor-parent',receipt_parent='#receipt-parent')
    with tx(growth_agent) as c:
        target=one(c,'SELECT * FROM lead.account_targets WHERE id=%s',(publish['target_id'],))
        c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s',
            (j({'browser_methods':{'publish':original,'reply':reply}}),target['id']))
        c.execute("UPDATE lead.authorization_grants SET allowed_actions='[\"publish\",\"reply\"]' WHERE id=%s",(publish['grant_id'],))
        submission_gate(c,actor,publish,claim)
        content=one(c,'SELECT * FROM lead.content_items WHERE id=%s',(publish['content_id'],))
    finish(claim,WorkResult(outcome='failed',failure_code='scope_changed'))
    data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'],operation='reply',
        parent_url=target['canonical_url']+'/posts/1')
    with tx(growth_agent) as c:
        ref=submit_work(c,growth_agent,TaskSpec(kind='channel',purpose='reply to the original post',
            account_id=social['id'],target_id=target['id'],payload=data.model_dump(mode='json'),
            review_at=utcnow()+timedelta(hours=1)))
    assert advance(ref)['claim']
    action=prepare_channel(growth_agent,ref['task_id'],data)
    assert action['type']=='reply' and action['intent']['browser_method']==reply
    assert publish['intent']['browser_method']==original
    capabilities=[item for item in build_context(growth_agent)['capabilities'] if item.get('method')=='native-browser-v1']
    assert len(capabilities)==1 and capabilities[0]['operations']==['publish','reply']
