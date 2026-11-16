"""Telegram group bridge: real scoped database/ledger, controlled page observations only."""
import asyncio
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from lead_app import account_connection, community_runtime, domain
from lead_app.account_intents import set_intent
from lead_app.db import j, one, tx, uid
from lead_app.execution_routes import register, request_route
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, execute, finish, recover_due
from lead_app.growth.store import submit_work
from test_growth_runtime import new_work

PEER = '-778899'
URL = 'https://t.me/privacy_fixture'


@pytest.fixture
def community(growth_agent):
    aid = str(growth_agent)
    new_work(aid)
    domain.connect_lab(aid)
    with tx(aid) as c:
        account = one(c, "UPDATE lead.social_accounts SET platform='telegram',external_account_id='fixture-self',automation_enabled=true WHERE platform='lab' RETURNING *")
        c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
            (j({'community_growth':{'enabled':True,'mode':'contextual_outreach'}}), aid))
        card = one(c, """INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,state,payload,expires_at)
            VALUES(lead.ctx_tenant(),%s,'manage_community','accepted',%s,now()+interval '1 day') RETURNING *""",
            (aid,j({'account_id':str(account['id']),'external_account_id':'fixture-self','source':'owner_strategy',
                   'allowed_actions':['join_public_group','read_recent_history','contextual_reply','relevant_promotion']})))
        c.execute("UPDATE lead.profile_leases SET epoch=1,state='held',expires_at=now()+interval '1 hour' WHERE profile_id=%s",(account['profile_id'],))
        session = one(c, """INSERT INTO lead.browser_sessions(tenant_id,agent_id,profile_id,node_id,actor_id,epoch,control_version,state)
            VALUES(lead.ctx_tenant(),%s,%s,'synthetic','fixture',1,1,'ready') RETURNING id""",(aid,account['profile_id']))
    set_intent(aid,account['id'],'running',source='owner_command',source_ref='fixture:original-owner',scope={'target_scope':'owned_managed','excluded_targets':['-99999']})
    register(aid,account['profile_id'])
    actor=SimpleNamespace(aid=aid,profile={'id':account['profile_id'],'slot':'default'},epoch=1,
        session=session['id'],lock=asyncio.Lock(),touch=lambda:None,page=object())
    return SimpleNamespace(aid=aid,account=account,card=card,actor=actor)


def work(env, operation, source=None):
    payload={'community_operation':operation}
    refs=[]
    if operation=='search':
        payload['queries']=['privacy tools']
    else:
        payload.update(group_task_id=source['task_id'],group_id=PEER)
        refs=[{'task_id':source['task_id'],'reason':'Use actual group observation'}]
    with tx(env.aid) as c:
        return submit_work(c,env.aid,TaskSpec(kind='account',purpose='community',account_id=env.account['id'],
            payload=payload,required_refs=refs,review_at=utcnow()+timedelta(hours=1)))


def source(env, group=None):
    ref=work(env,'search')
    claim=advance(ref)['claim']
    finish(claim,WorkResult(outcome='completed',output={'groups':[group or {'group_id':PEER,'canonical_url':URL,'title':'Privacy fixture'}]}))
    return ref


def inspected(env, *, joined=True):
    ref=work(env,'inspect',source(env))
    claim=advance(ref)['claim']
    group={'group_id':PEER,'canonical_url':URL,'title':'Privacy fixture','kind':'discussion',
        'rules':'Privacy tools discussion. Be helpful and respectful.','rules_hash':community_runtime.digest('Privacy tools discussion. Be helpful and respectful.'),
        'rules_status':'complete','automation_forbidden':False,'membership':'joined' if joined else 'not_joined',
        'can_reply':joined,'observed_at':utcnow().isoformat(),
        'messages':[{'mid':'10','excerpt':'How can I keep a chat private?','hash':community_runtime.digest('How can I keep a chat private?'),
                    'parent_url':URL+'/10'}]}
    finish(claim,WorkResult(outcome='completed',output={'group':group}))
    return ref,group


def test_original_owner_card_is_sufficient_and_revocation_is_enforced(community):
    with tx(community.aid) as c:
        intent,card=account_connection.community_authority(c,community.aid,community.account,'join')
        assert intent['scope']['target_scope']=='owned_managed' and card['id']==community.card['id']
        assert 'community' not in intent['scope']
        c.execute("UPDATE lead.interaction_cards SET state='canceled' WHERE id=%s",(card['id'],))
    with tx(community.aid) as c:
        assert account_connection.community_authority(c,community.aid,community.account,'inspect',required=False)[1] is None
        with pytest.raises(ValueError,match='community_owner_scope_missing'):
            account_connection.community_authority(c,community.aid,community.account,'join')


def test_group_reference_requires_dependency_and_preserves_exclusions(community):
    original=source(community)
    ref=work(community,'inspect',original)
    with tx(community.aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        group=account_connection.community_group(c,row,original['task_id'],PEER)
        row['input']['spec']['required_refs']=[]
        with pytest.raises(ValueError,match='community_group_dependency_required'):
            account_connection.community_group(c,row,original['task_id'],PEER)
        with pytest.raises(ValueError,match='target_not_permitted'):
            account_connection.community_group_allowed({'scope':{'excluded_targets':[PEER]}},group)


def test_join_identity_is_not_recreated_by_a_new_inspection(community):
    original,_=inspected(community,joined=False)
    first=work(community,'join',original)
    # A fresh inspection of the same real peer cannot mint a second join identity.
    with tx(community.aid) as c:
        second=submit_work(c,community.aid,TaskSpec(kind='account',purpose='community',account_id=community.account['id'],
            payload={'community_operation':'join','group_task_id':original['task_id'],'group_id':PEER},
            required_refs=[{'task_id':original['task_id'],'reason':'Recheck same group'}],dedupe_key=uid(),
            review_at=utcnow()+timedelta(hours=2)))
    assert first==second


def test_unproven_membership_completion_is_rejected(community):
    original,_=inspected(community,joined=False)
    ref=work(community,'join',original)
    claim=advance(ref)['claim']
    finish(claim,WorkResult(outcome='completed',output={'state':'confirmed','operation_key':'invented'}))
    with tx(community.aid) as c:
        assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(ref['task_id'],))['state']=='failed'


@pytest.mark.asyncio
async def test_unknown_membership_survives_pause_restart_and_route_change(community,monkeypatch):
    original,_=inspected(community,joined=False)
    ref=work(community,'join',original)
    claim=advance(ref)['claim']
    with tx(community.aid) as c:
        route=request_route(c,community.aid,'/community/step',{'task_id':ref['task_id']}).model_dump(mode='json')
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,community}',%s) WHERE id=%s",
            (j({'state':'dispatching','group_id':PEER,'canonical_url':URL,'operation_key':'original-membership','execution_ref':route}),ref['task_id']))
    async def lost(*args):
        raise OSError('Controlled lost membership acknowledgement')
    monkeypatch.setattr('lead_app.growth.tools.execute_tool',lost)
    result=await execute(claim)
    assert result['outcome']=='unknown'
    finish(claim,result)
    set_intent(community.aid,community.account['id'],'paused',source='owner_command',source_ref='fixture:pause')
    with tx(community.aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert row['state']=='reconciling' and row['result']['runtime']['community']['operation_key']=='original-membership'
    future=utcnow()+timedelta(minutes=16)
    assert advance(ref,now=future)['state']=='ready'
    recover_due(community.aid,now=future+timedelta(minutes=10))
    with tx(community.aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert row['state']=='reconciling' and row['result']['runtime']['community']['state']=='dispatching'


@pytest.mark.asyncio
async def test_join_uses_one_click_and_a_separate_member_receipt(community,monkeypatch):
    original,_=inspected(community,joined=False)
    ref=work(community,'join',original)
    claim=advance(ref)['claim']
    state={'joined':False,'clicks':0}
    def observed():
        return {'target_id':PEER,'title':'Privacy fixture','kind':'discussion','description':'Privacy tools discussion. Be helpful and respectful.',
            'joined':state['joined'],'can_reply':state['joined'],'history':[], 'dialogs':[],
            'join_button_count':0 if state['joined'] else 1,'apply_button_count':0,'controls':[],
            'editor_metadata':[{'editable':True}] if state['joined'] else []}
    async def read(*args,**kwargs):
        return observed()
    async def click(**kwargs):
        state.update(joined=True,clicks=state['clicks']+1)
    async def button(*args):
        return SimpleNamespace(click=click)
    async def controls(*args):
        return observed()
    monkeypatch.setattr(community_runtime,'observe_group',read)
    monkeypatch.setattr(community_runtime,'membership_button',button)
    monkeypatch.setattr(community_runtime,'membership_controls',controls)
    first=await community_runtime.growth_community_step(community.actor,ref['task_id'],'join',claim)
    second=await community_runtime.growth_community_step(community.actor,ref['task_id'],'join',claim)
    assert state['clicks']==1 and first['operation_key']==second['operation_key']
    finish(claim,WorkResult(outcome='completed',output=second))
    with tx(community.aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert row['state']=='succeeded' and row['result']['runtime']['community']['state']=='confirmed'
        assert one(c,'SELECT count(*) n FROM lead.actions WHERE task_id=%s',(ref['task_id'],))['n']==0
        assert one(c,"SELECT count(*) n FROM lead.content_items WHERE validation_result->>'growth_task_id'=%s",(ref['task_id'],))['n']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_ack',[False,True])
async def test_group_reply_uses_original_content_action_and_verified_attempt(community,monkeypatch,lost_ack):
    from lead_app import telegram
    from lead_app.growth.store import digest
    from lead_app.growth.tools import ChannelInput, prepare_channel
    question='How can I keep a chat private?'
    observation={'target_id':PEER,'title':'Privacy fixture','kind':'discussion','description':'Privacy tools discussion. Be helpful and respectful.',
        'joined':True,'can_reply':True,'history':[{'mid':'10','body':question,'own':False}],
        'dialogs':[],'join_button_count':0,'apply_button_count':0,'controls':[],'editor_metadata':[{'editable':True}]}
    async def read(*args,**kwargs):
        return observation
    monkeypatch.setattr(community_runtime,'observe_group',read)
    context=work(community,'inspect',source(community))
    inspect_claim=advance(context)['claim']
    observed=await community_runtime.growth_community_step(community.actor,context['task_id'],'inspect',inspect_claim)
    finish(inspect_claim,WorkResult(outcome='completed',output=observed))
    body={'body':'Check the documented privacy settings before sharing sensitive information.','title':'','format':'text','media':[]}
    with tx(community.aid) as c:
        content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
            VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(community.aid,j(body),digest(body)))
        data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'],operation='reply',
            parent_url=URL+'/10',community_context_task_id=context['task_id'])
        ref=submit_work(c,community.aid,TaskSpec(kind='channel',purpose='contextual_reply',account_id=community.account['id'],
            target_id=observed['target_id'],payload=data.model_dump(mode='json'),
            required_refs=[{'task_id':context['task_id'],'reason':'Reply to this exact observed question'}],review_at=utcnow()+timedelta(hours=1)))
    claim=advance(ref)['claim']
    action=prepare_channel(community.aid,ref['task_id'],data)
    with tx(community.aid) as c:
        publish_capability=one(c,"SELECT technical_state,evidence_ref FROM lead.platform_capabilities WHERE platform='telegram' AND action_type='publish' AND target_type='channel'")
    state={'sent':0}
    class Node:
        def locator(self,*args): return self
        def filter(self,**kwargs): return self
        async def count(self): return 1
        async def click(self,**kwargs): return None
    class Page(Node):
        async def evaluate(self,*args): return {'target_id':PEER}
    community.actor.page=Page()
    async def chat(*args): return Node()
    async def identity(*args): return {'external_account_id':'fixture-self'}
    async def baseline(*args): return '10'
    async def preview(*args): return True
    async def fill(*args): return None
    async def send(*args):
        state['sent']+=1
        if lost_ack:
            raise OSError('Controlled loss after the external send')
    async def receipt(*args,**kwargs):
        assert kwargs['parent_mid']=='10'
        return {'mid':'11'} if state['sent'] else None
    for name,function in (('selected_chat',chat),('inspect_account',identity),('baseline',baseline),('fill',fill),('submit',send)):
        monkeypatch.setattr(telegram,name,function)
    monkeypatch.setattr(community_runtime,'reply_preview_matches',preview)
    monkeypatch.setattr(community_runtime,'own_reply_receipt',receipt)
    if lost_ack:
        with pytest.raises(OSError,match='Controlled loss'):
            await community_runtime.execute_growth_reply(community.actor,action['id'],claim)
        finish(claim,WorkResult(outcome='unknown',evidence_refs=[{'kind':'action','id':action['id']}]))
        set_intent(community.aid,community.account['id'],'paused',source='owner_command',source_ref='fixture:pause-after-send')
        claim=advance(ref,now=utcnow()+timedelta(minutes=6))['claim']
    first=await community_runtime.execute_growth_reply(community.actor,action['id'],claim)
    second=await community_runtime.execute_growth_reply(community.actor,action['id'],claim)
    assert first['external_id']==second['external_id']==PEER+':11' and state['sent']==1
    finish(claim,WorkResult(outcome='completed',evidence_refs=[{'kind':'action','id':action['id']}]))
    with tx(community.aid) as c:
        assert one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
        assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(ref['task_id'],))['state']=='succeeded'
        assert one(c,'SELECT count(*) n FROM lead.action_attempts WHERE action_id=%s',(action['id'],))['n']==1
        assert one(c,"SELECT technical_state,evidence_ref FROM lead.platform_capabilities WHERE platform='telegram' AND action_type='publish' AND target_type='channel'")==publish_capability


def test_read_scope_does_not_require_write_delegation_and_stale_claim_is_rejected(community):
    ref=work(community,'search')
    claim=advance(ref)['claim']
    with tx(community.aid) as c:
        c.execute("UPDATE lead.interaction_cards SET state='canceled' WHERE id=%s",(community.card['id'],))
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert account_connection.community_task_scope(c,community.aid,row,claim,operation='search')[2] is None
        with pytest.raises(ValueError,match='community_task_scope_changed'):
            account_connection.community_task_scope(c,community.aid,row,{**claim,'attempt_token':'stale'},operation='search')


def test_latest_revoked_delegation_does_not_fall_back_to_an_older_live_card(community):
    with tx(community.aid) as c:
        c.execute("""INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,state,payload,expires_at,created_at)
            VALUES(lead.ctx_tenant(),%s,'manage_community','canceled',%s,now()+interval '1 day',now()+interval '1 second')""",
            (community.aid,j(community.card['payload'])))
        with pytest.raises(ValueError,match='community_owner_scope_missing'):
            account_connection.community_authority(c,community.aid,community.account,'reply')


@pytest.mark.parametrize('target_type,control',[('group','disabled'),('group','revoked'),('channel','disabled'),('channel','revoked')])
def test_observation_cannot_revive_a_stopped_peer_across_target_types(community,target_type,control):
    ref,group=inspected(community)
    with tx(community.aid) as c:
        target=one(c,"""INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,
            canonical_url,display_name,verified_permissions,state) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,'old target','{}',%s) RETURNING *""",
            (community.aid,community.account['id'],target_type,PEER,URL,'disabled' if control=='disabled' else 'active'))
        c.execute("""INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,limits,granted_by,revoked_at)
            VALUES(lead.ctx_tenant(),%s,%s,1,'["reply"]','{}','original owner',now())""",(community.aid,target['id']))
        task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        derived=account_connection.derive_community_group(c,community.aid,task,community.account,group)
        assert derived['write_scope_missing'] and derived['write_blocked_reason']=='community_target_'+('disabled' if control=='disabled' else 'grant_revoked')
        assert one(c,'SELECT * FROM lead.account_targets WHERE id=%s',(target['id'],))==target
        assert one(c,'SELECT count(*) n FROM lead.authorization_grants WHERE target_id=%s',(target['id'],))['n']==1
        assert one(c,'SELECT count(*) n FROM lead.account_targets WHERE account_id=%s AND external_target_id=%s',(community.account['id'],PEER))['n']==1


def test_derivation_only_rotates_its_own_current_grant(community):
    ref,group=inspected(community)
    with tx(community.aid) as c:
        task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        original=account_connection.derive_community_group(c,community.aid,task,community.account,group)
    set_intent(community.aid,community.account['id'],'running',source='owner_command',source_ref='fixture:scope-changed',
        scope={'target_scope':'owned_managed','excluded_targets':['-99999','-88888']})
    with tx(community.aid) as c:
        derived=account_connection.derive_community_group(c,community.aid,task,community.account,group)
        assert original['target_id']==derived['target_id'] and original['grant_id']!=derived['grant_id']
        assert one(c,'SELECT revoked_at FROM lead.authorization_grants WHERE id=%s',(original['grant_id'],))['revoked_at']
        c.execute("UPDATE lead.authorization_grants SET granted_by='original owner' WHERE id=%s",(derived['grant_id'],))
        grant=one(c,'SELECT * FROM lead.authorization_grants WHERE id=%s',(derived['grant_id'],))
        target=one(c,'SELECT * FROM lead.account_targets WHERE id=%s',(derived['target_id'],))
        blocked=account_connection.derive_community_group(c,community.aid,task,community.account,{**group,'title':'new title'})
        assert blocked['write_blocked_reason']=='community_target_has_independent_control'
        assert one(c,'SELECT * FROM lead.authorization_grants WHERE id=%s',(derived['grant_id'],))==grant
        assert one(c,'SELECT * FROM lead.account_targets WHERE id=%s',(derived['target_id'],))==target


@pytest.mark.parametrize('desired',['paused','revoked'])
def test_real_owner_account_control_preserves_only_original_pending_membership(community,desired):
    original,_=inspected(community,joined=False)
    pending=work(community,'join',original)
    advance(pending)
    with tx(community.aid) as c:
        ordinary=submit_work(c,community.aid,TaskSpec(kind='account',purpose='community',account_id=community.account['id'],
            payload={'community_operation':'search','queries':['another audience']},review_at=utcnow()+timedelta(hours=1)))
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,community}',%s) WHERE id=%s",
            (j({'state':'pending_approval','operation_key':'original-only'}),pending['task_id']))
    set_intent(community.aid,community.account['id'],desired,source='owner_command',source_ref='fixture:real-stop')
    with tx(community.aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(pending['task_id'],))
        assert row['state']=='running' and row['result']['runtime']['community']['operation_key']=='original-only'
        assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(ordinary['task_id'],))['state']=='canceled'
    recover_due(community.aid,now=utcnow()+timedelta(minutes=10))
    assert advance(pending,now=utcnow()+timedelta(minutes=12))['state']=='ready'


def test_archive_keeps_membership_history_without_restoring_account_access(community):
    original,_=inspected(community,joined=False)
    ref=work(community,'join',original)
    claim=advance(ref)['claim']
    with tx(community.aid) as c:
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,community}',%s) WHERE id=%s",
            (j({'state':'dispatching','operation_key':'archive-original'}),ref['task_id']))
    domain.set_control(community.aid,'archive')
    with tx(community.aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert row['state']=='running' and row['result']['runtime']['community']['operation_key']=='archive-original'
        account=one(c,'SELECT state,automation_enabled FROM lead.social_accounts WHERE id=%s',(community.account['id'],))
        assert account['state']=='disconnected' and not account['automation_enabled']
        with pytest.raises(LookupError):
            account_connection.community_task_scope(c,community.aid,row,claim,operation='join',reconcile=True)


@pytest.mark.parametrize('description,gap',[
    ('Privacy tools discussion. Be helpful and respectful.',None),
    ('Privacy tools discussion. Read the pinned rules before participating.','referenced_pinned_rules'),
    ('Privacy tools discussion. Read rules https://example.test/rules before participating.','referenced_external_rules'),
    ('Privacy tools','substantive_public_description'),
    ('','substantive_public_description'),
])
def test_description_rules_do_not_claim_unread_pin_or_external_evidence(description,gap):
    group=community_runtime._growth_group({'description':description,'target_id':PEER,'title':'Privacy fixture',
        'kind':'discussion','joined':True,'can_reply':True,'history':[]},{'canonical_url':URL})
    assert group['rules_read_scope']=='public_description'
    assert group['rules_status']==('unresolved' if gap else 'complete')
    assert group['unread_rule_sources']==([gap] if gap else [])
    assert group['rules_next_action']==('select_another_group' if gap else None)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',reason='Explicit local browser fixtures only')
async def test_actual_dom_reply_receipt_requires_the_exact_parent():
    from playwright.async_api import async_playwright
    async with async_playwright() as manager:
        browser=await manager.chromium.launch(headless=True,executable_path=manager.chromium.executable_path)
        try:
            page=await browser.new_page()
            await page.set_content('''<div class="chat active" data-type="chat">
                <div class="bubble is-out" data-mid="9" data-reply-to-mid="10"><div class="message">Controlled reply</div></div>
                <div class="bubble is-out" data-mid="11" data-reply-to-mid="99"><div class="message">Controlled reply</div></div>
                </div>''')
            assert await community_runtime.own_reply_receipt(page,'Controlled reply','10',parent_mid='10') is None
            await page.locator('[data-mid="11"]').evaluate("node=>node.dataset.replyToMid='10'")
            assert await community_runtime.own_reply_receipt(page,'Controlled reply','10',parent_mid='10')=={'mid':'11'}
            await page.locator('[data-mid="11"]').evaluate("node=>node.removeAttribute('data-reply-to-mid')")
            assert await community_runtime.own_reply_receipt(page,'Controlled reply','10',parent_mid='10') is None
        finally:
            await browser.close()
