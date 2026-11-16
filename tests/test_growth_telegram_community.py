"""Telegram group bridge: real scoped database/ledger, controlled page observations only."""
import asyncio
import os
import threading
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
    route=register(aid,account['profile_id'])
    actor=SimpleNamespace(aid=aid,profile={'id':account['profile_id'],'slot':'default'},epoch=1,
        session=session['id'],lock=asyncio.Lock(),touch=lambda:None,page=object(),execution_ref=route)
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
    first=await community_runtime.growth_community_step(community.actor,ref['task_id'],'join',claim,execution_ref=community.actor.execution_ref)
    second=await community_runtime.growth_community_step(community.actor,ref['task_id'],'join',claim,execution_ref=community.actor.execution_ref)
    assert state['clicks']==1 and first['operation_key']==second['operation_key']
    finish(claim,WorkResult(outcome='completed',output=second))
    with tx(community.aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert row['state']=='succeeded' and row['result']['runtime']['community']['state']=='confirmed'
        assert one(c,'SELECT count(*) n FROM lead.actions WHERE task_id=%s',(ref['task_id'],))['n']==0
        assert one(c,"SELECT count(*) n FROM lead.content_items WHERE validation_result->>'growth_task_id'=%s",(ref['task_id'],))['n']==0


@pytest.mark.asyncio
async def test_pause_committed_before_final_agent_lock_prevents_join_click(community,monkeypatch):
    original,_=inspected(community,joined=False)
    ref=work(community,'join',original)
    claim=advance(ref)['claim']
    entered,release=threading.Event(),threading.Event()
    state={'locks':0,'clicks':0}
    original_one=account_connection.one
    def barrier(c,sql,params=()):
        if 'FROM lead.agents' in sql and 'FOR SHARE' in sql:
            state['locks']+=1
            if state['locks']==3:
                # The final join barrier has not acquired its Agent row lock.
                # A second connection commits the real owner pause here.
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Owner pause did not release the join barrier')
        return original_one(c,sql,params)
    observation={'target_id':PEER,'title':'Privacy fixture','kind':'discussion',
        'description':'Privacy tools discussion. Be helpful and respectful.','joined':False,'can_reply':False,
        'history':[],'dialogs':[],'join_button_count':1,'apply_button_count':0,'controls':[],'editor_metadata':[]}
    async def read(*args,**kwargs):
        return observation
    async def click(**kwargs):
        state['clicks']+=1
    async def button(*args):
        return SimpleNamespace(click=click)
    async def controls(*args):
        return {**observation,'joined':True,'join_button_count':0,'can_reply':True,'editor_metadata':[{'editable':True}]}
    monkeypatch.setattr(account_connection,'one',barrier)
    monkeypatch.setattr(community_runtime,'observe_group',read)
    monkeypatch.setattr(community_runtime,'membership_button',button)
    monkeypatch.setattr(community_runtime,'membership_controls',controls)
    task=asyncio.create_task(asyncio.to_thread(lambda:asyncio.run(
        community_runtime.growth_community_step(community.actor,ref['task_id'],'join',claim,execution_ref=community.actor.execution_ref))))
    try:
        assert await asyncio.to_thread(entered.wait,5), 'Final join barrier was not reached'
        domain.set_control(community.aid,'pause')
    finally:
        release.set()
    with pytest.raises(ValueError,match='community_task_scope_changed'):
        await asyncio.wait_for(task,5)
    assert state['locks']==3 and state['clicks']==0
    with tx(community.aid) as c:
        current=one(c,'SELECT desired_state,control_version FROM lead.agents WHERE id=%s',(community.aid,))
        assert current['desired_state']=='paused' and current['control_version']>claim['control_version']
        row=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert row['result']['runtime']['community']['state']=='dispatching'


@pytest.mark.asyncio
@pytest.mark.parametrize('control',['enabled','mode','allow_join','route','cadence'])
async def test_existing_owner_controls_and_selected_route_fence_join(community,monkeypatch,control):
    from lead_app.community_growth import GrowthConfig, set_strategy
    original,_=inspected(community,joined=False)
    ref=work(community,'join',original)
    claim=advance(ref)['claim']
    state={'clicks':0}
    async def read(*args,**kwargs):
        if control=='route':
            register(community.aid,community.account['profile_id'],executor_ref='replacement')
        return {'target_id':PEER,'title':'Privacy fixture','kind':'discussion',
            'description':'Privacy tools discussion. Be helpful and respectful.','joined':False,'can_reply':False,
            'history':[],'dialogs':[],'join_button_count':1,'apply_button_count':0,'controls':[],'editor_metadata':[]}
    async def click(**kwargs):
        state['clicks']+=1
    async def button(*args):
        return SimpleNamespace(click=click)
    monkeypatch.setattr(community_runtime,'observe_group',read)
    monkeypatch.setattr(community_runtime,'membership_button',button)
    if control in ('enabled','mode','allow_join'):
        config=GrowthConfig(enabled=True,mode='contextual_outreach').model_dump()
        config[control]='discovery' if control=='mode' else False
        set_strategy(community.aid,config)
    elif control=='cadence':
        with tx(community.aid) as c:
            c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
                (j({'execution_cadence':{'mode':'paused'}}),community.aid))
    expected={'enabled':'community_outreach_disabled','mode':'community_outreach_disabled',
        'allow_join':'community_join_disabled','route':'execution_route_changed','cadence':'cadence_paused'}[control]
    with pytest.raises(ValueError,match=expected):
        await community_runtime.growth_community_step(community.actor,ref['task_id'],'join',claim,execution_ref=community.actor.execution_ref)
    assert state['clicks']==0
    if control=='route':
        with tx(community.aid) as c:
            saved=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(ref['task_id'],))['result']['runtime']['community']
            assert saved['execution_ref']==community.actor.execution_ref.model_dump(mode='json')
            assert request_route(c,community.aid,'/community/step',{'task_id':ref['task_id']})==community.actor.execution_ref


@pytest.mark.asyncio
async def test_rpc_injects_its_selected_route_and_unknown_keeps_that_route(community,monkeypatch):
    from lead_app.activities import browser_rpc
    original,_=inspected(community,joined=False)
    ref=work(community,'join',original)
    calls=[]
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return None
        async def post(self,url,*,json,headers):
            calls.append((url,json))
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'state':'observed'})
    monkeypatch.setattr('lead_app.activities.httpx.AsyncClient',Client)
    data={'task_id':ref['task_id'],'operation':'join','execution_ref':{'arbitrary':'model supplied'}}
    await browser_rpc('/agents/'+community.aid+'/community/step',data)
    assert calls[0][1]['execution_ref']==community.actor.execution_ref.model_dump(mode='json')
    register(community.aid,community.account['profile_id'],executor_ref='replacement')
    with tx(community.aid) as c:
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime}',%s) WHERE id=%s",
            (j({'community':{'state':'unknown','execution_ref':calls[0][1]['execution_ref']}}),ref['task_id']))
    await browser_rpc('/agents/'+community.aid+'/community/step',data)
    assert calls[1][0]==calls[0][0] and calls[1][1]['execution_ref']==calls[0][1]['execution_ref']


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
        async def inner_text(self): return body['body']
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
    async def history(*args,**kwargs): return observation['history']
    monkeypatch.setattr(community_runtime,'read_history',history)
    monkeypatch.setattr(community_runtime,'own_reply_receipt',receipt)
    if lost_ack:
        with pytest.raises(OSError,match='Controlled loss'):
            await community_runtime.execute_growth_reply(community.actor,action['id'],claim)
        finish(claim,WorkResult(outcome='unknown',evidence_refs=[{'kind':'action','id':action['id']}]))
        set_intent(community.aid,community.account['id'],'paused',source='owner_command',source_ref='fixture:pause-after-send')
        from lead_app.community_growth import set_strategy
        set_strategy(community.aid,{'enabled':False,'mode':'discovery','allow_join':False,'allow_reply':False,'allow_promotion':False})
        claim=advance(ref,now=utcnow()+timedelta(minutes=6))['claim']
    first=await community_runtime.execute_growth_reply(community.actor,action['id'],claim)
    second=await community_runtime.execute_growth_reply(community.actor,action['id'],claim)
    assert first['external_id']==second['external_id']==PEER+':11' and state['sent']==1
    finish(claim,WorkResult(outcome='completed',evidence_refs=[{'kind':'action','id':action['id']}]))
    with tx(community.aid) as c:
        assert one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
        assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(ref['task_id'],))['state']=='succeeded'
        assert one(c,'SELECT count(*) n FROM lead.action_attempts WHERE action_id=%s',(action['id'],))['n']==1
        assert one(c,'SELECT count(*) n FROM lead.authorization_grants WHERE target_id=%s',(observed['target_id'],))['n']==1
        assert one(c,"SELECT technical_state,evidence_ref FROM lead.platform_capabilities WHERE platform='telegram' AND action_type='publish' AND target_type='channel'")==publish_capability


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS')!='1',reason='Explicit local browser fixtures only')
@pytest.mark.parametrize('lost',['preview','editor'])
async def test_real_dom_fill_changes_cannot_send_an_unbound_or_changed_reply(community,monkeypatch,lost):
    from lead_app import telegram
    from lead_app.growth.store import digest
    from lead_app.growth.tools import ChannelInput, prepare_channel
    from playwright.async_api import async_playwright
    question='How can I keep a chat private?'
    observation={'target_id':PEER,'title':'Privacy fixture','kind':'discussion','description':'Privacy tools discussion. Be helpful and respectful.',
        'joined':True,'can_reply':True,'history':[{'mid':'10','body':question,'own':False}],
        'dialogs':[],'join_button_count':0,'apply_button_count':0,'controls':[],'editor_metadata':[{'editable':True}]}
    async def read(*args,**kwargs): return observation
    async def identity(*args): return {'external_account_id':'fixture-self'}
    monkeypatch.setattr(community_runtime,'observe_group',read)
    monkeypatch.setattr(telegram,'inspect_account',identity)
    context=work(community,'inspect',source(community))
    inspect_claim=advance(context)['claim']
    observed=await community_runtime.growth_community_step(community.actor,context['task_id'],'inspect',inspect_claim)
    finish(inspect_claim,WorkResult(outcome='completed',output=observed))
    body={'body':'Check the documented privacy settings.','title':'','format':'text','media':[]}
    with tx(community.aid) as c:
        content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
            VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(community.aid,j(body),digest(body)))
        data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'],operation='reply',
            parent_url=URL+'/10',community_context_task_id=context['task_id'])
        ref=submit_work(c,community.aid,TaskSpec(kind='channel',purpose='contextual_reply',account_id=community.account['id'],
            target_id=observed['target_id'],payload=data.model_dump(mode='json'),
            required_refs=[{'task_id':context['task_id'],'reason':'Use actual source'}],review_at=utcnow()+timedelta(hours=1)))
    claim=advance(ref)['claim']
    action=prepare_channel(community.aid,ref['task_id'],data)
    async with async_playwright() as manager:
        browser=await manager.chromium.launch(headless=True,executable_path=manager.chromium.executable_path)
        try:
            page=await browser.new_page()
            community.actor.page=page
            await page.set_content('''<div class="chat active" data-type="chat">
                <div class="chat-info"><span class="peer-title" data-peer-id="-778899">Privacy fixture</span></div>
                <div class="bubble" data-mid="10" oncontextmenu="event.preventDefault();document.querySelector('.btn-menu-item').style.display='block'">
                    <div class="message">How can I keep a chat private?</div></div>
                <div class="reply-wrapper" style="display:none"><span class="reply-subtitle">How can I keep a chat private?</span></div>
                <div class="input-message-input" contenteditable="true" style="width:300px;min-height:30px"></div>
                <button class="btn-send send" data-sends="0" onclick="this.dataset.sends=String(Number(this.dataset.sends)+1)">Send</button></div>
                <button class="btn-menu-item" style="display:none" onclick="document.querySelector('.reply-wrapper').style.display='block';this.style.display='none'">Reply</button>''')
            await page.locator('.input-message-input').evaluate("""(node,lost)=>node.addEventListener('input',()=>{
                if(lost==='preview')document.querySelector('.reply-wrapper')?.remove();
                else node.textContent='Changed draft after fill';
            })""",lost)
            with pytest.raises(ValueError,match='community_reply_parent_changed' if lost=='preview' else 'community_reply_draft_changed'):
                await community_runtime.execute_growth_reply(community.actor,action['id'],claim)
            assert await page.locator('.btn-send').get_attribute('data-sends')=='0'
            with tx(community.aid) as c:
                assert one(c,'SELECT state FROM lead.actions WHERE id=%s',(action['id'],))['state']=='dispatching'
                assert one(c,'SELECT count(*) n FROM lead.action_attempts WHERE action_id=%s',(action['id'],))['n']==1
        finally:
            await browser.close()


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


@pytest.mark.parametrize('link',['www.promo.example','promo.example','ftp://promo.example/product'])
@pytest.mark.parametrize('permission',['none','asked','rule'])
def test_all_existing_chat_link_forms_require_source_permission(community,link,permission):
    ref,group=inspected(community)
    if permission=='rule':
        group['rules']+=' Ads are allowed.'
    with tx(community.aid) as c:
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{output,output,group}',%s) WHERE id=%s",(j(group),ref['task_id']))
        task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        derived=account_connection.derive_community_group(c,community.aid,task,community.account,group)
        target=one(c,'SELECT * FROM lead.account_targets WHERE id=%s',(derived['target_id'],))
        grant=one(c,'SELECT * FROM lead.authorization_grants WHERE id=%s',(derived['grant_id'],))
        intent=one(c,'SELECT * FROM lead.growth_read_intent(%s)',(community.account['id'],))
        parent=URL+'/10'
        action={'id':uid(),'type':'reply','intent':{'body':'Try '+link,'format':'text','media':[],'parent_url':parent,
            'telegram_community':{'version':account_connection.COMMUNITY_ADAPTER,'group_id':PEER,'canonical_url':URL,
                'parent_url':parent,'delegation_id':str(community.card['id']),'observed_at':group['observed_at'],
                'context_task_id':ref['task_id'],'reply_to_mid':'10','source_excerpt':'Please share a website link' if permission=='asked' else 'How do I keep a chat private?'}}}
        if permission=='none':
            with pytest.raises(ValueError,match='community_link_not_requested'):
                community_runtime.growth_reply_gate(c,community.actor,action,task,community.account,target,intent,grant)
        else:
            community_runtime.growth_reply_gate(c,community.actor,action,task,community.account,target,intent,grant)


@pytest.mark.parametrize('field,value',[('allow_reply',False),('review_replies_only',True),('mode','discovery'),('enabled',False)])
def test_current_reply_strategy_does_not_inherit_an_older_owner_card(community,field,value):
    from lead_app.community_growth import GrowthConfig, set_strategy
    ref,group=inspected(community)
    config=GrowthConfig(enabled=True,mode='contextual_outreach').model_dump()
    config[field]=value
    set_strategy(community.aid,config)
    with tx(community.aid) as c:
        with pytest.raises(ValueError,match='community_(reply|outreach)_disabled'):
            account_connection.community_authority(c,community.aid,community.account,'reply')
        task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert account_connection.derive_community_group(c,community.aid,task,community.account,group)['write_scope_missing']
        assert one(c,"SELECT count(*) n FROM lead.account_targets WHERE external_target_id=%s",(PEER,))['n']==0
        assert one(c,"SELECT count(*) n FROM lead.interaction_cards WHERE type='manage_community'")['n']==1


@pytest.mark.parametrize('disabled',['strategy','delegation'])
def test_product_promotion_needs_current_switch_and_original_promotion_action(community,disabled):
    from lead_app.community_growth import set_strategy
    if disabled=='strategy':
        set_strategy(community.aid,{'enabled':True,'mode':'contextual_outreach','allow_promotion':False})
    else:
        with tx(community.aid) as c:
            payload={**community.card['payload'],'allowed_actions':['join_public_group','read_recent_history','contextual_reply']}
            c.execute('UPDATE lead.interaction_cards SET payload=%s WHERE id=%s',(j(payload),community.card['id']))
    with tx(community.aid) as c:
        assert account_connection.community_authority(c,community.aid,community.account,'reply')[1]
        with pytest.raises(ValueError,match='community_promotion_disabled' if disabled=='strategy' else 'community_owner_scope_missing'):
            account_connection.community_authority(c,community.aid,community.account,'promote')


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
