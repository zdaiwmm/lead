"""New publication points bind to the immutable original Action and content."""
from lead_app.db import j, one, tx
from lead_app.growth.contracts import WorkResult
from lead_app.growth.runtime import advance, finish
from lead_app.growth.tools import execute_channel, prepare_channel
from lead_app.local_executor_tools import _content_artifacts
from test_growth_channel_intention import PARENT_ONE, submit_variant
from test_growth_unknown_receipts import facebook_work, real_submit
from test_pixel_events import pixel  # noqa: F401


def test_publication_registers_one_original_point_on_reentry(request,monkeypatch):
    monkeypatch.setattr('lead_app.analytics.public_origin',lambda:'https://lead.example')
    aid = request.getfixturevalue('pixel')[0]
    with facebook_work(aid,"https://site.example/") as (ref,data,account):
        advance(ref)
        action = prepare_channel(aid,ref['task_id'],data)
        assert prepare_channel(aid,ref['task_id'],data)['id']==action['id']
        with tx(aid) as c:
            link = one(c,'SELECT * FROM lead.tracking_links WHERE content_id=%s',(action['content_id'],))
            final = one(c,'SELECT * FROM lead.content_items WHERE id=%s',(action['content_id'],))
            target = one(c,'SELECT canonical_url FROM lead.account_targets WHERE id=%s',(action['target_id'],))
            assert one(c,'SELECT count(*) n FROM lead.tracking_links WHERE intent_key=%s',('growth:'+ref['task_id'],))['n']==1
        assert str(link['account_id'])==str(account['id'])
        assert link['placement']=='post' and link['placement_ref']==target['canonical_url']
        assert link['intent_key']==action['operation_key']=='growth:'+ref['task_id']
        assert link['destination_url']=='https://site.example/'
        assert link['public_code']==final['body']['marker'] and link['public_code'] in action['intent']['body']
        assert str(link['content_id'])==str(action['content_id'])
        assert final['content_hash']==action['intent']['content_hash']


def test_reply_point_uses_parent_and_preserves_previous_publication(request,monkeypatch):
    monkeypatch.setattr('lead_app.analytics.public_origin',lambda:'https://lead.example')
    aid = request.getfixturevalue('pixel')[0]
    with facebook_work(aid,'https://site.example/') as (original,data,account):
        claim = advance(original)['claim']
        publication = prepare_channel(aid,original['task_id'],data)
        with tx(aid) as c:
            original_link = one(c,'SELECT * FROM lead.tracking_links WHERE content_id=%s',(publication['content_id'],))
            target = one(c,'SELECT canonical_url FROM lead.account_targets WHERE id=%s',(publication['target_id'],))
            url = target['canonical_url']
            reply = {'version':'native-browser-v1','account_id':str(account['id']),
                'binding_version':account['binding_version'],'target_url':url,'self_url':url,
                'operations':['reply'],'role':'ordinary_member',
                'selectors':{key:'#'+key for key in ('self_subject','editor_target','editor_body','submit','receipt_author','receipt_body','receipt_url','editor_parent','receipt_parent')},
                'request':{'url':url+'/replies','method':'POST','body_kind':'json','content_field':'body','parent_field':'parent'},
                'evidence_refs':['fixture:self-and-rule-observation'],
                'platform_permission':{'state':'approved','evidence_refs':['fixture:local-site-permission']}}
            c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s',
                (j({'browser_methods':{'reply':reply}}),publication['target_id']))
            c.execute("UPDATE lead.authorization_grants SET allowed_actions='[\"publish\",\"reply\"]' WHERE target_id=%s AND revoked_at IS NULL",(publication['target_id'],))
        # End the unsent fixture's write lease before preparing a second action.
        finish(claim,WorkResult(outcome='failed',failure_code='scope_changed'))
        ref,data = submit_variant(aid,original,'reply',PARENT_ONE)
        assert advance(ref)['state']=='ready'
        action = prepare_channel(aid,ref['task_id'],data)
        with tx(aid) as c:
            link = one(c,'SELECT * FROM lead.tracking_links WHERE content_id=%s',(action['content_id'],))
            assert one(c,'SELECT * FROM lead.tracking_links WHERE id=%s',(original_link['id'],))==original_link
        assert link['placement']=='reply' and link['placement_ref']==PARENT_ONE
        assert link['intent_key']==action['operation_key']
        assert link['public_code']!=original_link['public_code']


async def test_confirmed_channel_exposes_actual_tracked_body_to_original_reader(request,monkeypatch):
    monkeypatch.setattr('lead_app.analytics.public_origin',lambda:'https://lead.example')
    aid = request.getfixturevalue('pixel')[0]
    with facebook_work(aid,'https://site.example/') as (ref,data,account):
        await real_submit(aid,monkeypatch,ref,data,account)
        with tx(aid) as c:
            row = one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
            action = one(c,'SELECT * FROM lead.actions WHERE task_id=%s',(ref['task_id'],))
        async def original_receipt(path,body):
            assert str(action['id'])==body['action_id']
            return {'state':'confirmed','external_id':action['external_id']}
        monkeypatch.setattr('lead_app.activities.browser_rpc',original_receipt)
        result = await execute_channel(aid,row,data,{'ref':ref,'attempt_token':'readback-only'})
        assert result.outcome=='completed' and len(result.artifacts)==1
        with tx(aid) as c:
            resolved,limited = _content_artifacts(c,aid,result.model_dump(mode='json'))
        assert not limited and resolved[0]['available']
        assert resolved[0]['hash']==action['intent']['content_hash']
        assert resolved[0]['body']['body']==action['intent']['body']
        point = result.output['distribution_point']
        assert point['platform']=='facebook' and point['placement']=='post'
        assert str(point['account_id'])==str(account['id'])
