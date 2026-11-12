"""Production task/command interfaces with controlled provider/website transport.

No strategy rows are inserted by this test. Real model quality is evaluated by
the separate explicit 27-run fixture; this test verifies the complete runtime.
"""
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
from fastapi.testclient import TestClient
from lead_app import account_connection, activities, analytics, api, browser, domain
from lead_app.adapters.catalog import SPECS
from lead_app.config import database_kwargs
from lead_app.db import all_rows, one, tx, uid
from lead_app.growth import agent, tools
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    utcnow,
)
from lead_app.growth.outcomes import summarize
from lead_app.growth.runtime import advance, ensure_decision, execute, growth_finish
from lead_app.security import token
from lead_app.website_brief import ConfirmedBrief
from psycopg.rows import dict_row
from test_adapters import ROOT as FIXTURES
from test_adapters import TARGETS, FixtureSite


async def test_product_connection_decision_content_operation_value_next_decision(monkeypatch,process_pending_clicks):
    monkeypatch.setattr(analytics,'resolve_public',lambda url:None)
    monkeypatch.setattr(analytics,'public_origin',lambda:'https://lead.example')
    created=api.confirm_website(ConfirmedBrief(client_id=uuid4(),product_name='A generic quote builder',
        product_url='https://site.example/',introduction='Independent makers compare quote line items and export a usable quote.',
        features=['Compare line items','Export a quote'],customers=['Independent makers']))
    aid=created['id']
    client=TestClient(api.app)
    client.cookies.set('lead_session',token('owner'))
    headers={'Origin':'http://127.0.0.1:3100','X-Lead-Request':'1'}
    cid=client.post(f'/api/agents/{aid}/analytics',headers=headers,json={'site_url':'https://site.example'}).json()['connection']['id']
    key=client.post(f'/api/agents/{aid}/analytics/server-key',headers=headers,json={'connection_id':cid}).json()['key']
    definition=client.post(f'/api/agents/{aid}/analytics/events/configure',headers=headers,json={
        'connection_id':cid,'name':'Activation','label':'首次获得核心价值','method':'server','expected_revision':1,
        'definition':'首次比较并导出可用报价'})
    assert definition.status_code==200,definition.text
    changed=client.post(f'/api/agents/{aid}/growth/commands',headers=headers,json={'command_id':uid(),
        'expected_revision':1,'kind':'objective.update','payload':{'description':'Makers compare and export a first usable quote',
        'event_ref':{'connection_id':cid,'event_name':'Activation','definition_revision':definition.json()['revision']}}})
    assert changed.status_code==200,changed.text
    class OwnedPage(FixtureSite):
        async def route(self,route):
            if '/__fixture/' in route.request.url:
                return await super().route(route)
            html=(FIXTURES/'facebook.html').read_text().replace('__POSTS__',__import__('json').dumps(self.posts))
            html+=f'<a href="{TARGETS["facebook"]}">Your managed Page</a>'
            await route.fulfill(body=html,content_type='text/html')
    site=OwnedPage('facebook')
    gateway=browser.Gateway()
    original_get=gateway.get
    routed=set()
    async def get(*args,**kwargs):
        actor=await original_get(*args,**kwargs)
        if id(actor.context) not in routed:
            await actor.context.route('**/*',site.route)
            await actor.context.add_cookies([{'name':'c_user','value':'10001','url':'https://www.facebook.com'}])
            routed.add(id(actor.context))
        return actor
    monkeypatch.setattr(gateway,'get',get)
    monkeypatch.setattr(browser,'gateway',gateway)
    async def rpc(project,profile,path,body=None,**kwargs):
        assert project==aid
        if path=='open':
            return await browser.open_browser(aid,browser.OpenRequest(**body))
        if path=='account/observe':
            return await browser.observe_account(UUID(aid),browser.OpenRequest(**body))
        if path=='account/discover':
            return await browser.discover_account_targets(UUID(aid),browser.DiscoverRequest(**body))
        raise AssertionError(path)
    monkeypatch.setattr(account_connection,'rpc',rpc)
    async def publish_rpc(path,data):
        assert path==f'/agents/{aid}/publish'
        return await browser.publish_action(UUID(aid),browser.PublishRequest(**data))
    monkeypatch.setattr(activities,'browser_rpc',publish_rpc)
    async def fetch(url,**kwargs):
        return {'url':url,'title':'Quote comparison','text':'Independent makers can compare quote line items and export a usable quote.','links':[]}
    monkeypatch.setattr(tools,'fetch_product',fetch)
    decisions=[]
    async def provider(schema,context,**kwargs):
        if schema is tools.ContentDraft:
            return schema(body='Compare quote line items and export a usable quote in one place.',title='Compare your quote',
                claim_checks=[{'claim':'Compare and export a quote','source':{'kind':'business','id':context['product']['id']},'basis':'owner_asserted'}])
        if schema is tools.ResearchAssessment:
            return schema(findings=[],unresolved=['Additional evidence is still required.'])
        assert schema is DecisionProposal
        decisions.append(context)
        goal={'revision':context['goal']['revision']}
        exp=next((item for item in context['experiments'] if item['state']=='active'),None)
        content=next((item for item in context['work'] if item['type']=='growth.content' and item['state']=='succeeded'),None)
        commands=[]
        if context['outcomes'].get('new_units')==1:
            assert context['outcomes']['visits']['visitors']==1
            commands=[Command(command_key='close-initial',group_key='learning',kind='experiment.close',
                experiment_id=exp['id'],expected_revision=exp['revision'],goal_ref=goal,
                evidence_refs=[{'kind':'event','id':context['outcomes']['evidence_refs'][0]}],
                payload={'state':'inconclusive','reason':'One first value establishes possibility, not scale.'}),
                Command(command_key='next-question',group_key='next-distribution',kind='experiment.create',goal_ref=goal,
                    payload=ExperimentSpec(question_type='distribution',question='Can an independent audience discover the useful entry?',
                    hypothesis='An appropriate public venue can surface the quote comparison entry',learning_signal='Observed distribution evidence',
                    token_limit=100000,review_at=utcnow()+timedelta(hours=1)).model_dump(mode='json')),
                Command(command_key='next-source',group_key='next-distribution',kind='task.create',goal_ref=goal,
                    payload=TaskSpec(kind='research',purpose='distribution',payload={'urls':['https://site.example/'],
                    'question':'What independent discovery evidence is available?'},review_at=utcnow()+timedelta(hours=1)).model_dump(mode='json'))]
        elif content:
            artifact=content['output']['artifacts'][0]
            target=context['targets'][0]
            account=next(item for item in context['accounts'] if str(item['id'])==str(target['account_id']))
            commands=[Command(command_key='publish-useful-entry',group_key='initial',kind='task.create',goal_ref=goal,
                account_id=account['id'],expected_binding=account['binding_version'],experiment_id=exp['id'],
                payload=TaskSpec(kind='channel',purpose='publish',experiment_id=exp['id'],account_id=account['id'],target_id=target['id'],
                    payload={'content_id':artifact['id'],'content_hash':artifact['hash'],'format':'text'},
                    review_at=utcnow()+timedelta(hours=1)).model_dump(mode='json'))]
        else:
            commands=[Command(command_key='first-question',group_key='initial',kind='experiment.create',goal_ref=goal,
                payload=ExperimentSpec(question_type='value',question='Can a visitor compare and export a usable quote?',
                    hypothesis='A useful factual entry leads to first product value',learning_signal='Trusted first-value receipt',
                    token_limit=100000,review_at=utcnow()+timedelta(hours=1)).model_dump(mode='json')),
                Command(command_key='useful-entry',group_key='initial',kind='task.create',goal_ref=goal,
                    payload=TaskSpec(kind='content',purpose='explain',payload={'brief':'Explain quote comparison using provided facts',
                        'entry':'https://site.example/'},review_at=utcnow()+timedelta(hours=1)).model_dump(mode='json'))]
        return schema(context_key=context['context_key'],rationale='Follow actual product facts and the latest observed bottleneck.',commands=commands)
    monkeypatch.setattr(agent,'run_model',provider)
    monkeypatch.setattr(tools,'run_model',provider)
    async def work(kind,purpose=None):
        with tx(aid) as c:
            row=one(c,"""SELECT * FROM lead.tasks WHERE type=%s AND state='planned'
                AND (%s::text IS NULL OR input#>>'{spec,purpose}'=%s) ORDER BY created_at LIMIT 1""",('growth.'+kind,purpose,purpose))
        assert row,(kind,purpose)
        ref={'agent_id':aid,'task_id':str(row['id']),'task_generation':row['input']['task_generation']}
        claim=advance(ref)['claim']
        result=await execute(claim)
        assert result['outcome']=='completed',result
        await growth_finish({'agent_id':aid,'claim':claim,'result':result})
        return str(row['id'])
    def wake(reason,**extra):
        return ensure_decision(aid,[{'reason':reason,**extra}])
    with psycopg.connect(**database_kwargs(owner=True),row_factory=dict_row) as c:
        cap=one(c,"SELECT * FROM lead.platform_capabilities WHERE platform='facebook' AND action_type='publish' AND target_type='page'")
        c.execute("UPDATE lead.platform_capabilities SET permission_state='approved',technical_state='unverified',adapter_version=%s WHERE platform='facebook' AND action_type='publish' AND target_type='page'",(SPECS['facebook'].version,))
    try:
        connection=await account_connection.begin(aid,'facebook',new_account=True)
        await work('account','connect')
        account_connection.connected(aid,str(connection['account_id']))
        await work('account','discover')
        product_task=await work('research','product')
        wake('evidence_added',task_id=product_task)
        await work('decision')
        draft=await work('content')
        wake('evidence_added',task_id=draft)
        await work('decision')
        publication=await work('channel')
        assert len(site.posts)==1
        with tx(aid) as c:
            link=one(c,"SELECT l.* FROM lead.actions a JOIN lead.tracking_links l ON l.content_id=a.content_id WHERE a.task_id=%s",(publication,))
        from urllib.parse import parse_qs, urlsplit
        redirect=client.get('/r/'+link['public_code'],follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'})
        click=parse_qs(urlsplit(redirect.headers['location']).query)['lc'][0]
        from test_pixel_events import send
        anonymous=uid()
        page=send((aid,cid,client,headers,[],key),anonymous_id=anonymous,ref=link['public_code'],click_token=click)
        assert page.status_code==200,page.text
        event=client.post('/collect/server/events',headers={'Authorization':'Bearer '+key},json={
            'connection_id':cid,'event_id':uid(),'event_name':'Activation','user_id':'synthetic-new-maker',
            'lead_attribution':page.json()['attribution'],
            'properties':{'first_business_value':True,'value_qualified':True,'rule_version':'activation-v'+str(definition.json()['revision'])}})
        assert event.status_code==200,event.text
        process_pending_clicks(aid)
        assert summarize(aid)['new_units']==1
        with tx(aid) as c:
            receipt=one(c,"SELECT payload FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='business_receipt' ORDER BY occurred_at DESC LIMIT 1")
        assert receipt
        ensure_decision(aid,[receipt['payload']])
        await work('decision')
        assert len(decisions)==3 and len(site.posts)==1
        with tx(aid) as c:
            assert one(c,"SELECT count(*) n FROM lead.growth_experiments WHERE state='closed'")['n']==1
            assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type='growth.research' AND state='planned'")['n']==1
            assert len(all_rows(c,'SELECT id FROM lead.actions'))==1
    finally:
        await gateway.close(aid)
        domain.set_control(aid,'archive')
        with psycopg.connect(**database_kwargs(owner=True)) as c:
            c.execute("UPDATE lead.platform_capabilities SET permission_state=%s,technical_state=%s,adapter_version=%s,evidence_ref=%s,reviewed_at=%s WHERE platform='facebook' AND action_type='publish' AND target_type='page'",
                tuple(cap[name] for name in ('permission_state','technical_state','adapter_version','evidence_ref','reviewed_at')))
