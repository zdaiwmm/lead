"""Legacy operations use the owner card's actual per-operation scope."""
from types import SimpleNamespace

import pytest
from lead_app import community_receipts, community_runtime, telegram
from lead_app.db import j, one, tx, uid
from test_growth_telegram_community import community as community


@pytest.mark.parametrize('operation,promote,allowed,missing',[
    ('observe',False,True,None),('receipt',False,True,None),('scan_pending',False,True,None),
    ('join',False,True,None),('reply',False,True,None),('reply',True,False,None),
    ('profile_bio',False,False,None),('leave_restricted',False,False,None),
    ('join',False,False,'join_public_group'),('reply',False,False,'contextual_reply'),
    ('observe',False,False,'read_recent_history'),
])
def test_nonpromotional_owner_card_is_checked_for_the_requested_operation(community,operation,promote,allowed,missing):
    actions=[a for a in ['join_public_group','read_recent_history','contextual_reply'] if a!=missing]
    task={'id':uid(),'type':'community.engage','state':'running','result':{},'input':{
        'account_id':str(community.account['id']),'external_account_id':community.account['external_account_id'],
        'delegation_id':str(community.card['id']),'control_version':1,'config_revision':1,
        'reply':{'body':'Useful grounded reply','promote':promote}}}
    actor=SimpleNamespace(aid=community.aid,gate=lambda *a,**k:None)
    with tx(community.aid) as c:
        c.execute('UPDATE lead.interaction_cards SET payload=jsonb_set(payload,\'{allowed_actions}\',%s) WHERE id=%s',
            (j(actions),community.card['id']))
        if allowed:
            account,_=community_runtime.guard(actor,c,task,operation=operation,read_only=operation in {'observe','receipt','scan_pending'})
            assert account['id']==community.account['id']
        else:
            with pytest.raises(ValueError,match='社群策略、账号身份或委托已变化'):
                community_runtime.guard(actor,c,task,operation=operation)


@pytest.mark.asyncio
async def test_original_reply_receipt_can_be_persisted_with_only_read_scope(community,monkeypatch):
    peer='-777777'
    async def bound(*args):
        return {'target_id':peer}
    async def receipt(*args):
        return {'mid':'6'}
    async def view(*args):
        return {'target_id':peer}
    monkeypatch.setattr(community_receipts,'bound_view',bound)
    monkeypatch.setattr(community_runtime,'own_reply_receipt',receipt)
    actor=SimpleNamespace(aid=community.aid,page=SimpleNamespace(evaluate=view),gate=lambda *a,**k:None)
    with tx(community.aid) as c:
        # This consumer belongs to the original legacy owner, not the growth owner.
        c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state - 'growth_runtime' WHERE id=%s",(community.aid,))
        c.execute('UPDATE lead.interaction_cards SET payload=jsonb_set(payload,\'{allowed_actions}\',%s) WHERE id=%s',
            (j(['read_recent_history']),community.card['id']))
        task=one(c,"""INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
            VALUES(lead.ctx_tenant(),%s,'community.engage','reconciling',%s,%s,%s,now()) RETURNING *""",
            (community.aid,'fixture:'+uid(),j({'account_id':str(community.account['id']),
                'external_account_id':community.account['external_account_id'],'delegation_id':str(community.card['id']),
                'control_version':1,'config_revision':1,'reply':{'body':'Original exact reply','promote':True}}),
                j({'reply_state':'unknown','baseline_mid':'5','target_id':peer})))
    result=await community_receipts.reconcile(actor,task,community.account,peer,10)
    assert result=={'state':'confirmed','external_id':peer+':6'}
    with tx(community.aid) as c:
        saved=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(task['id'],))['result']
        assert saved['reply_state']=='confirmed' and saved['reply_external_id']==peer+':6'
        assert one(c,'SELECT count(*) n FROM lead.actions WHERE task_id=%s',(task['id'],))['n']==0


@pytest.mark.asyncio
async def test_profile_save_rechecks_its_original_scope_after_opening_settings(community,monkeypatch):
    desired='Brand profile'
    filled=[]
    async def noop(*args):
        pass
    async def not_editing(*args):
        return False
    async def read(*args):
        return 'Previous profile'
    async def fill(value):
        filled.append(value)
        raise AssertionError('Revoked scope must stop before editing')
    async def identity(*args):
        return {'external_account_id':community.account['external_account_id']}
    async def open_settings(*args):
        with tx(community.aid) as c:
            c.execute('UPDATE lead.interaction_cards SET payload=jsonb_set(payload,\'{allowed_actions}\',%s) WHERE id=%s',
                (j(['read_recent_history','join_public_group','contextual_reply']),community.card['id']))
        return object(),SimpleNamespace(evaluate=read,fill=fill)
    monkeypatch.setattr(telegram,'own_bio_field',open_settings)
    monkeypatch.setattr(telegram,'inspect_account',identity)
    page=SimpleNamespace(url='https://web.telegram.org/k/',bring_to_front=noop,is_closed=lambda:True,
        locator=lambda *a:SimpleNamespace(evaluate_all=not_editing))
    actor=SimpleNamespace(aid=community.aid,page=page,gate=lambda *a,**k:None)
    task={'id':uid(),'type':'community.engage','state':'running','result':{},'input':{
        'account_id':str(community.account['id']),'external_account_id':community.account['external_account_id'],
        'delegation_id':str(community.card['id']),'control_version':1,'config_revision':1}}
    with tx(community.aid) as c:
        c.execute('UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,\'{community_growth,profile_bio}\',%s) WHERE id=%s',
            (j(desired),community.aid))
        account,config=community_runtime.guard(actor,c,task,operation='profile_bio')
    assert await community_runtime.update_own_bio(actor,task,account,config)=={'state':'pending'}
    assert filled==[]
    with tx(community.aid) as c:
        saved=one(c,'SELECT onboarding_state FROM lead.agents WHERE id=%s',(community.aid,))['onboarding_state']
        assert '社群策略、账号身份或委托已变化' in saved['community_profile_receipt']['reason']
