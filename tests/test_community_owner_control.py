"""Explicit owner scope and cadence choices through the original strategy control."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from lead_app import account_connection, community_runtime, domain
from lead_app.account_intents import set_intent
from lead_app.api import community_strategy_update
from lead_app.community_growth import GrowthConfig
from lead_app.community_participation import ParticipationPolicy, quota_due
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.growth.contracts import WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from lead_app.growth.tools import AccountInput, ChannelInput
from lead_app.scope import service_scope
from pydantic import ValidationError
from test_growth_telegram_community import PEER, inspected, work
from test_growth_telegram_community import community as community


def prepare_existing_owner(env, *, policy=None):
    with tx(env.aid) as c:
        payload={**env.card['payload'],'allowed_actions':['join_public_group','read_recent_history','contextual_reply']}
        c.execute('UPDATE lead.interaction_cards SET payload=%s WHERE id=%s',(j(payload),env.card['id']))
        config={'enabled':True,'mode':'contextual_outreach','allow_promotion':False,'participation_policy':policy or {}}
        cadence={'mode':'active','policy_version':1,'parameter_origins':{
            name:{'origin':'unresolved'} for name in ('combined_per_24h','joins_per_24h','replies_per_24h','reply_interval_minutes')}}
        c.execute('UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s',
            (j({'community_growth':config,'execution_cadence':cadence}),env.aid))
        return payload,cadence


def apply_explicit_owner(env):
    with service_scope():
        return community_strategy_update(UUID(env.aid),GrowthConfig(enabled=True,mode='contextual_outreach',
            allow_promotion=True,participation_policy=ParticipationPolicy(quota_mode='agent')))


def test_explicit_owner_refreshes_only_the_bound_delegation_and_keeps_original_controls(community):
    old_payload,cadence=prepare_existing_owner(community)
    with tx(community.aid) as c:
        c.execute("UPDATE lead.authorization_grants SET revoked_at=now() WHERE revoked_at IS NULL")
        c.execute("UPDATE lead.account_targets SET state='disabled'")
        targets=all_rows(c,'SELECT * FROM lead.account_targets ORDER BY id')
        grants=all_rows(c,'SELECT * FROM lead.authorization_grants ORDER BY id')
        intent=one(c,'SELECT * FROM lead.growth_read_intent(%s)',(community.account['id'],))
    result=apply_explicit_owner(community)
    assert result['config']['allow_promotion'] and result['config']['participation_policy']['quota_mode']=='agent'
    with tx(community.aid) as c:
        cards=all_rows(c,"SELECT * FROM lead.interaction_cards WHERE type='manage_community' ORDER BY created_at,id")
        assert len(cards)==2
        old=next(card for card in cards if card['id']==community.card['id'])
        fresh=next(card for card in cards if card['id']!=community.card['id'])
        assert old['state']=='canceled' and old['payload']==old_payload
        assert fresh['state']=='accepted' and fresh['payload']['source']=='owner_strategy'
        assert fresh['payload']['account_id']==str(community.account['id'])
        assert fresh['payload']['external_account_id']==community.account['external_account_id']
        assert fresh['payload']['binding_version']==community.account['binding_version']
        assert fresh['payload']['supersedes']==str(old['id']) and fresh['payload']['delegation_version']==2
        assert fresh['payload']['allowed_actions']==old_payload['allowed_actions']+['relevant_promotion']
        agent=one(c,'SELECT onboarding_state FROM lead.agents WHERE id=%s',(community.aid,))
        assert agent['onboarding_state']['execution_cadence']=={**cadence,'mode':'paused','pause_reason':'strategy_changed'}
        assert all_rows(c,'SELECT * FROM lead.account_targets ORDER BY id')==targets
        assert all_rows(c,'SELECT * FROM lead.authorization_grants ORDER BY id')==grants
        assert one(c,'SELECT * FROM lead.growth_read_intent(%s)',(community.account['id'],))==intent
        audit=one(c,"SELECT actor_type,payload FROM lead.audit_events WHERE type='community.strategy_updated' ORDER BY occurred_at DESC,id DESC LIMIT 1")
        assert audit['actor_type']=='service' and audit['payload']['_explicit_policy_fields']==['quota_mode']
        assert account_connection.community_authority(c,community.aid,community.account,'promote')[1]['id']==fresh['id']
        community_runtime.growth_community_quota(c,community.aid,community.account,uid(),'join')
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type LIKE 'community.%%'")['n']==0
    apply_explicit_owner(community)
    with tx(community.aid) as c:
        assert one(c,"SELECT count(*) n FROM lead.interaction_cards WHERE type='manage_community'")['n']==2


@pytest.mark.parametrize('mode',['combined','per_action','agent'])
def test_omitted_policy_and_default_promotion_do_not_expand_or_reset_owner_choices(community,mode):
    policy={'quota_mode':mode,'combined_per_24h':7,'joins_per_24h':4,'replies_per_24h':6,'reply_interval_minutes':45}
    old_payload,_=prepare_existing_owner(community,policy=policy)
    community_strategy_update(UUID(community.aid),GrowthConfig(enabled=True,mode='contextual_outreach'))
    with tx(community.aid) as c:
        cards=all_rows(c,"SELECT * FROM lead.interaction_cards WHERE type='manage_community'")
        assert len(cards)==1 and cards[0]['state']=='accepted' and cards[0]['payload']==old_payload
        saved=one(c,'SELECT onboarding_state FROM lead.agents WHERE id=%s',(community.aid,))['onboarding_state']['community_growth']
        assert saved['participation_policy']==ParticipationPolicy.model_validate(policy).model_dump()
        audit=one(c,"SELECT payload FROM lead.audit_events WHERE type='community.strategy_updated' ORDER BY occurred_at DESC,id DESC LIMIT 1")
        assert audit['payload']['_explicit_policy_fields']==[]
        with pytest.raises(ValueError,match='community_owner_scope_missing'):
            account_connection.community_authority(c,community.aid,community.account,'promote')


def test_explicit_agent_choice_keeps_existing_numeric_policy_values(community):
    policy={'quota_mode':'per_action','joins_per_24h':3,'replies_per_24h':5,'reply_interval_minutes':90}
    prepare_existing_owner(community,policy=policy)
    apply_explicit_owner(community)
    with tx(community.aid) as c:
        saved=one(c,'SELECT onboarding_state FROM lead.agents WHERE id=%s',(community.aid,))['onboarding_state']['community_growth']
        assert saved['participation_policy']==ParticipationPolicy.model_validate({**policy,'quota_mode':'agent'}).model_dump()


def test_new_default_delegation_does_not_imply_promotion_authority(community):
    prepare_existing_owner(community)
    with tx(community.aid) as c:
        c.execute("UPDATE lead.interaction_cards SET state='canceled' WHERE id=%s",(community.card['id'],))
    community_strategy_update(UUID(community.aid),GrowthConfig(enabled=True,mode='contextual_outreach'))
    with tx(community.aid) as c:
        fresh=one(c,"SELECT * FROM lead.interaction_cards WHERE type='manage_community' AND state='accepted'")
        assert fresh['payload']['account_id']==str(community.account['id'])
        assert fresh['payload']['external_account_id']==community.account['external_account_id']
        assert 'relevant_promotion' not in fresh['payload']['allowed_actions']
        with pytest.raises(ValueError,match='community_owner_scope_missing'):
            account_connection.community_authority(c,community.aid,community.account,'promote')


def test_existing_quota_modes_retain_their_limits_and_agent_has_no_operating_quota():
    now=utcnow()
    attempts=[('join',now-timedelta(minutes=2)),('reply',now-timedelta(minutes=1))]
    assert quota_due({'quota_mode':'combined','combined_per_24h':2},'join',attempts,now)['reason']=='daily_participation_limit'
    assert quota_due({'quota_mode':'per_action','joins_per_24h':2},'join',attempts,now) is None
    assert quota_due({'quota_mode':'per_action','replies_per_24h':2},'reply',attempts,now)['reason']=='daily_participation_limit'
    assert quota_due({'quota_mode':'combined','combined_per_24h':0},'join',[],now)['reason']=='participation_disabled'
    for operation in ('join','reply'):
        assert quota_due({'quota_mode':'agent','combined_per_24h':0,'joins_per_24h':0,'replies_per_24h':0},operation,attempts*50,now) is None
    with pytest.raises(ValidationError):
        AccountInput(community_operation='search',queries=['public tools'],quota_mode='agent')
    with pytest.raises(ValidationError):
        ChannelInput(content_id=uid(),content_hash='0'*64,quota_mode='agent')


@pytest.mark.asyncio
@pytest.mark.parametrize('control',['project','account'])
async def test_agent_quota_keeps_modern_pause_before_any_join(community,control):
    prepare_existing_owner(community)
    apply_explicit_owner(community)
    original,_=inspected(community,joined=False)
    ref=work(community,'join',original)
    claim=advance(ref)['claim']
    if control=='project':
        domain.set_control(community.aid,'pause')
    else:
        set_intent(community.aid,community.account['id'],'paused',source='owner_command',source_ref='fixture:modern-pause')
    with pytest.raises(ValueError,match='community_task_scope_changed'):
        await community_runtime.growth_community_step(community.actor,ref['task_id'],'join',claim,execution_ref=community.actor.execution_ref)


@pytest.mark.asyncio
async def test_agent_mode_operates_after_canonical_legacy_pause_and_only_reads_an_unknown_join(community,monkeypatch):
    prepare_existing_owner(community)
    apply_explicit_owner(community)
    original,_=inspected(community,joined=False)
    ref=work(community,'join',original)
    claim=advance(ref)['claim']
    state={'joined':False,'clicks':0}
    async def read(*args,**kwargs):
        return {'target_id':PEER,'title':'Privacy fixture','kind':'discussion','description':'Privacy tools discussion. Be helpful and respectful.',
            'joined':state['joined'],'can_reply':state['joined'],'history':[],'dialogs':[],'join_button_count':1,'apply_button_count':0,'controls':[]}
    async def click(**kwargs):
        state.update(joined=True,clicks=state['clicks']+1)
        raise OSError('Controlled loss after the one membership click')
    async def button(*args): return SimpleNamespace(click=click)
    monkeypatch.setattr(community_runtime,'observe_group',read)
    monkeypatch.setattr(community_runtime,'membership_button',button)
    with pytest.raises(OSError,match='Controlled loss'):
        await community_runtime.growth_community_step(community.actor,ref['task_id'],'join',claim,execution_ref=community.actor.execution_ref)
    finish(claim,WorkResult(outcome='unknown'))
    set_intent(community.aid,community.account['id'],'paused',source='owner_command',source_ref='fixture:pause-original-membership')
    community_strategy_update(UUID(community.aid),GrowthConfig(enabled=False,allow_join=False,allow_reply=False,allow_promotion=False))
    resumed=advance(ref,now=utcnow()+timedelta(minutes=6))
    receipt=await community_runtime.growth_community_step(community.actor,ref['task_id'],'join',resumed['claim'],execution_ref=community.actor.execution_ref)
    assert receipt['state']=='confirmed' and state['clicks']==1
    finish(resumed['claim'],WorkResult(outcome='completed',output=receipt))
    with tx(community.aid) as c:
        assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(ref['task_id'],))['state']=='succeeded'
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type LIKE 'community.%%'")['n']==0
        assert one(c,'SELECT count(*) n FROM lead.actions WHERE task_id=%s',(ref['task_id'],))['n']==0
