"""Persisted funnel facts in the selected disposable verification database."""
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from lead_app import analytics, domain
from lead_app.analytics_ingest import process_ingest, scoped_tx, work_args
from lead_app.api import app
from lead_app.community_feedback import probe_cookie
from lead_app.db import j, one, tx, uid
from lead_app.distribution import register
from lead_app.growth.contracts import utcnow
from lead_app.growth.outcomes import distribution_trigger, summarize
from lead_app.scope import current_tenant
from lead_app.security import token
from test_pixel_events import pixel, send  # noqa: F401


@pytest.fixture
def project(monkeypatch):
    monkeypatch.setattr(domain, 'emit', lambda *a, **k: None)
    monkeypatch.setattr(analytics, 'resolve_public', lambda url: None)
    return domain.create_agent(runtime_engine='growth')


def client():
    value = TestClient(app)
    value.cookies.set('lead_session', token('owner'))
    return value


def link(aid, *, content=None, account=None, placement='manual', intent=None, location=None):
    with tx(aid) as c:
        row = register(c, aid, 'telegram', 'https://site.example/', intent=intent or 'fixture:'+uid(),
                       placement=placement, account=account, content=content, location=location)
        return one(c, 'SELECT id,public_code FROM lead.tracking_links WHERE id=%s', (row['id'],))


def experiment(aid, at):
    with tx(aid) as c:
        return one(c, """INSERT INTO lead.growth_experiments(tenant_id,agent_id,question_key,variation_key,specification,created_at)
            VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s) RETURNING id""",
            (aid,uid(),uid(),j({'review_at':(at+timedelta(hours=1)).isoformat(),'activation_ref':None}),
             at-timedelta(hours=2)))['id']


def publication(aid, exp, at, *, purpose='business', state='confirmed', placement='post', content=None):
    with tx(aid) as c:
        profile = one(c, 'SELECT id FROM lead.browser_profiles LIMIT 1')['id']
        account = one(c, """INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,external_account_id,state)
            VALUES(lead.ctx_tenant(),%s,%s,'telegram',%s,'connected') RETURNING id""",(aid,profile,uid()))['id']
        canonical='https://telegram.example/channel/'+uid()
        target = one(c, """INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,canonical_url,display_name,verified_permissions,state)
            VALUES(lead.ctx_tenant(),%s,%s,'channel',%s,%s,'Fixture','{}','active') RETURNING id""",(aid,account,uid(),canonical))['id']
        grant = one(c, """INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,granted_by)
            VALUES(lead.ctx_tenant(),%s,%s,1,'["publish"]','fixture') RETURNING id""",(aid,target))['id']
        if content is None:
            content = one(c, """INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                VALUES(lead.ctx_tenant(),%s,1,'{"body":"Fixture"}','[]','{}',%s) RETURNING id""",(aid,uid()))['id']
    row = {'aid':aid,'target':target,'grant':grant,'content':content,'account':account,'canonical':canonical}
    row['task']=add_action(row,exp,at,purpose=purpose,state=state,operation='reply' if placement=='reply' else 'publish')
    row.update(link(aid,content=content,account=account,placement=placement,intent='growth:'+str(row['task']),location=canonical))
    return row


def add_action(row, exp, at, *, purpose='business', state='confirmed', operation='publish'):
    with tx(row['aid']) as c:
        task = one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at,experiment_id)
            VALUES(lead.ctx_tenant(),%s,'growth.channel','succeeded',%s,'{}',now(),%s) RETURNING id""",
            (row['aid'],uid(),exp))['id']
        c.execute("""INSERT INTO lead.actions(tenant_id,agent_id,task_id,target_id,content_id,grant_id,operation_key,type,intent,
            expected_control_version,expected_config_revision,state,confirmed_at,external_id,evidence_ref,created_at)
            SELECT tenant_id,id,%s,%s,%s,%s,%s,%s,%s,control_version,config_revision,%s,%s,%s,'synthetic_receipt',%s
            FROM lead.agents WHERE id=%s""",
            (task,row['target'],row['content'],row['grant'],'growth:'+str(task),operation,
             j({'growth_runtime':1,'platform':'telegram','publication_purpose':purpose,'parent_url':row['canonical']}),
             state,at if state=='confirmed' else None,uid() if state=='confirmed' else None,at-timedelta(seconds=1),row['aid']))
        return task


def visit(aid, row, at, *, source='redirect', exclusion=None, anonymous=None):
    with tx(aid) as c:
        c.execute("""INSERT INTO lead.visit_sessions(tenant_id,agent_id,tracking_link_id,source,source_session_id,
            trust_level,qualified,exclusion_reason,rule_version,started_at,last_seen_at,anonymous_id)
            VALUES(lead.ctx_tenant(),%s,%s,%s,%s,'redirect_observed',NULL,%s,'fixture',%s,%s,%s)""",
            (aid,row['id'],source,uid(),exclusion,at,at,anonymous or uid()))


def test_no_pixel_keeps_processed_clicks_exclusions_and_retry_identity(project, process_pending_clicks):
    row, http = link(project), client()
    assert http.get('/r/'+row['public_code'],follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'}).status_code==302
    assert http.get('/r/'+row['public_code'],follow_redirects=False,headers={'User-Agent':'previewbot'}).status_code==302
    http.cookies.set('lead_click_browser',probe_cookie(project))
    assert http.get('/r/'+row['public_code'],follow_redirects=False,headers={'User-Agent':'LeadCoverageBot/1'}).status_code==302
    before=summarize(project)
    assert before['shortlinks']['coverage']=='pending' and before['shortlinks']['clicks']==0
    with scoped_tx(current_tenant(),project,'live') as c:
        pending=one(c,"SELECT * FROM lead.analytics_ingest_events WHERE state='pending' ORDER BY received_at LIMIT 1")
        args=work_args(pending)
    process_pending_clicks(project)
    after=summarize(project)
    assert after['shortlinks']['clicks']==1 and after['shortlinks']['unattributed_clicks']==1
    assert after['shortlinks']['excluded']=={'known_bot':1,'coverage_probe':1,'other':0}
    assert after['shortlinks']['observed_browser_devices']==1
    assert after['shortlinks']['coverage']=='recorded'
    assert after['visits']['page_views'] is None and after['event_funnel']==[]
    assert after['new_units'] is None and after['measurement']=='missing'
    assert process_ingest(args)['state']=='processed'
    again=summarize(project,as_of=utcnow()+timedelta(seconds=2))
    assert again['shortlinks']['clicks']==1 and again['signals_revision']==after['signals_revision']
    http.cookies.set('lead_click_browser',uid().replace('-',''))
    http.get('/r/'+row['public_code'],follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'})
    process_pending_clicks(project)
    assert summarize(project)['signals_revision']!=after['signals_revision']


def test_experiment_clicks_are_causal_scoped_not_fanned_out_or_diagnostic(project):
    at=utcnow()
    first,second=experiment(project,at),experiment(project,at)
    normal=publication(project,first,at-timedelta(minutes=20))
    add_action(normal,first,at-timedelta(minutes=10))
    other=publication(project,second,at-timedelta(minutes=20),placement='reply')
    diagnostic=publication(project,first,at-timedelta(minutes=20),purpose='diagnostic')
    unknown=publication(project,first,at-timedelta(minutes=20),state='unknown')
    legacy=publication(project,first,at-timedelta(minutes=20),placement=None)
    for row in (normal,other,diagnostic,unknown,legacy):
        visit(project,row,at)
    visit(project,normal,at-timedelta(minutes=30))  # Before confirmation.
    visit(project,normal,at+timedelta(minutes=2))  # Beyond as_of.
    visit(project,normal,at,source='site:fixture')  # Not a shortlink request.
    another=domain.create_agent(runtime_engine='growth')
    visit(another,link(another),at)
    observed=summarize(project,first,as_of=at+timedelta(minutes=1))
    short=observed['shortlinks']
    assert short['clicks']==short['business_clicks']==1
    assert short['diagnostic_clicks']==1 and short['unattributed_clicks']==0
    assert short['links']['total']==3 and short['links']['legacy_unknown']==0
    assert {item['id'] for item in short['links']['items']}=={str(r['id']) for r in (normal,diagnostic,unknown)}
    assert short['window']['start']==(at-timedelta(hours=2)).isoformat()
    assert observed['new_units'] is None
    later=summarize(project,first,as_of=at+timedelta(hours=2))['shortlinks']
    assert later['window']['end']==(at+timedelta(hours=1)).isoformat()
    assert later['clicks']==2
    assert summarize(project,second,as_of=at+timedelta(minutes=1))['shortlinks']['clicks']==1
    background=summarize(project,as_of=at+timedelta(minutes=1))['shortlinks']
    assert background['links']['legacy_unknown']==1 and background['unattributed_clicks']==3


def test_pixel_and_events_extend_the_funnel_without_relabeling_clicks(request, process_pending_clicks):
    source=request.getfixturevalue('pixel')
    aid,cid,http,headers,markers,_=source
    http.get('/r/'+markers[0],follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'})
    process_pending_clicks(aid)
    clicks=summarize(aid)
    assert clicks['shortlinks']['clicks']==1 and clicks['visits']['page_views']==0
    assert clicks['new_units'] is None
    assert send(source).status_code==200
    arrival=summarize(aid)
    assert arrival['shortlinks']['clicks']==1 and arrival['visits']['page_views']==1
    assert arrival['signals_revision']!=clicks['signals_revision']
    assert arrival['visits']['scope']==arrival['event_funnel_scope']=='project_context'
    definition=one_definition(aid,cid)
    test=http.post(f'/api/agents/{aid}/analytics/events/test',headers=headers,json={
        'definition_id':str(definition['id']),'expected_revision':definition['revision']}).json()
    assert send(source,'CompleteRegistration',user_id='test-user',test_code=test['test_code']).status_code==200
    assert summarize(aid)['signals_revision']==arrival['signals_revision']
    assert send(source,'CompleteRegistration',user_id='live-user').status_code==200
    result=summarize(aid)
    assert result['shortlinks']['clicks']==1 and result['signals_revision']!=arrival['signals_revision']
    assert sum(item['client_events'] for item in result['event_funnel'] if item['event_name']=='CompleteRegistration')==1
    assert result['new_units'] is None and result['measurement']=='missing'
    exp=experiment(aid,utcnow())
    scoped=summarize(aid,exp)
    assert scoped['visits']['coverage']=='unknown_attribution' and scoped['visits']['page_views'] is None
    assert scoped['visits']['scope']=='experiment'
    assert scoped['event_funnel']==[] and scoped['event_funnel_scope']=='experiment_unattributed'


def one_definition(aid,cid):
    with tx(aid) as c:
        return one(c,"SELECT id,revision FROM lead.pixel_definitions WHERE connection_id=%s AND name='CompleteRegistration'",(cid,))


def test_link_and_point_details_are_bounded_without_truncating_totals(project):
    at=utcnow()
    for index in range(22):
        row=link(project,placement='point_'+chr(97+index))
        visit(project,row,at+timedelta(seconds=1))
    short=summarize(project,as_of=at+timedelta(minutes=1))['shortlinks']
    assert short['clicks']==short['links']['total']==short['points']['total']==22
    assert len(short['links']['items'])==len(short['points']['items'])==20
    assert short['links']['truncated'] and short['points']['truncated']


def test_shared_content_cannot_steal_a_point_across_experiments_or_accounts(project):
    at=utcnow()
    first,second=experiment(project,at),experiment(project,at)
    original=publication(project,first,at-timedelta(minutes=20))
    reused=publication(project,second,at-timedelta(minutes=20),content=original['content'],placement='reply')
    task=add_action(original,first,at-timedelta(minutes=10))
    mismatch=link(project,content=original['content'],account=reused['account'],placement='post',
                  intent='growth:'+str(task),location=original['canonical'])
    for row in (original,reused,mismatch):
        visit(project,row,at)
    one=summarize(project,first,as_of=at+timedelta(minutes=1))['shortlinks']
    two=summarize(project,second,as_of=at+timedelta(minutes=1))['shortlinks']
    assert one['clicks']==one['business_clicks']==two['clicks']==two['business_clicks']==1
    assert {item['id'] for item in one['links']['items']}=={str(original['id'])}
    assert {item['id'] for item in two['links']['items']}=={str(reused['id'])}
    project_signals=summarize(project,as_of=at+timedelta(minutes=1))['shortlinks']
    assert project_signals['clicks']==3 and project_signals['business_clicks']==2
    assert project_signals['unattributed_clicks']==project_signals['links']['unassigned']==1


def test_diagnostic_identity_is_local_to_its_point_and_does_not_rewrite_old_clicks(project):
    at=utcnow()
    exp=experiment(project,at)
    business=publication(project,exp,at-timedelta(minutes=20))
    before_confirm=publication(project,exp,at-timedelta(minutes=5),purpose='diagnostic',state='prepared',
                               content=business['content'])
    after_click=publication(project,exp,at+timedelta(minutes=10),purpose='diagnostic',content=business['content'])
    for row in (business,before_confirm,after_click):
        visit(project,row,at)
    # Another later Action reuses the content but owns no registered point.
    add_action(business,exp,at+timedelta(minutes=20),purpose='diagnostic')
    result=summarize(project,exp,as_of=at+timedelta(minutes=30))['shortlinks']
    assert result['clicks']==result['business_clicks']==1
    assert result['diagnostic_clicks']==1
    project_result=summarize(project,as_of=at+timedelta(minutes=30))['shortlinks']
    assert project_result['clicks']==2 and project_result['business_clicks']==1
    assert project_result['unattributed_clicks']==1 and project_result['diagnostic_clicks']==1


def test_distribution_trigger_requires_processed_live_causal_business_point(project, process_pending_clicks):
    at=utcnow()
    exp=experiment(project,at)
    normal=publication(project,exp,at-timedelta(minutes=20))
    diagnostic=publication(project,exp,at-timedelta(minutes=20),purpose='diagnostic',state='prepared')
    unknown=publication(project,exp,at-timedelta(minutes=20),state='unknown')
    legacy=publication(project,exp,at-timedelta(minutes=20),placement=None)
    with tx(project) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s",(project,))
    http=client()
    for row in (normal,diagnostic,unknown,legacy):
        assert http.get('/r/'+row['public_code'],follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'}).status_code==302
    http.get('/r/'+normal['public_code'],follow_redirects=False,headers={'User-Agent':'previewbot'})
    http.cookies.set('lead_click_browser',probe_cookie(project))
    http.get('/r/'+normal['public_code'],follow_redirects=False,headers={'User-Agent':'LeadCoverageBot/1'})
    with scoped_tx(current_tenant(),project,'live') as c:
        ids=[row['id'] for row in c.execute("SELECT id FROM lead.analytics_ingest_events WHERE source_kind='redirect'").fetchall()]
    assert ids and all(distribution_trigger(project,ingest) is None for ingest in ids)
    process_pending_clicks(project)
    facts=[distribution_trigger(project,ingest) for ingest in ids]
    emitted=[fact for fact in facts if fact]
    assert len(emitted)==1
    fact=emitted[0]
    assert fact=={'reason':'distribution_signal','event_name':'ShortLinkClick','account_id':str(normal['account']),
        'experiment_id':str(exp),'task_id':str(normal['task']),'ingest_id':fact['ingest_id']}
    assert distribution_trigger(project,fact['ingest_id'])==fact
    another=domain.create_agent(runtime_engine='growth')
    with tx(another) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s",(another,))
    assert distribution_trigger(another,fact['ingest_id']) is None
    with tx(project) as c:
        c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s",(project,))
    assert distribution_trigger(project,fact['ingest_id']) is None


def test_distribution_trigger_pageview_requires_counted_original_point(request, process_pending_clicks):
    source=request.getfixturevalue('pixel')
    aid,_,http,_,_,_=source
    at=utcnow()
    exp=experiment(aid,at)
    point=publication(aid,exp,at-timedelta(minutes=20))
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running',onboarding_state=onboarding_state || %s WHERE id=%s",
                  (j({'growth_runtime':{'decision':{'engine':'growth'}}}),aid))
    redirected=http.get('/r/'+point['public_code'],follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'})
    proof=parse_qs(urlsplit(redirected.headers['location']).query)['lc'][0]
    event=uid()
    assert send(source,event_id=event,ref=point['public_code'],click_token=proof).status_code==200
    unassigned=uid()
    assert send(source,event_id=unassigned).status_code==200
    process_pending_clicks(aid)
    with scoped_tx(current_tenant(),aid,'live') as c:
        assigned=one(c,'SELECT id FROM lead.analytics_ingest_events WHERE event_id=%s',(event,))['id']
        unknown=one(c,'SELECT id FROM lead.analytics_ingest_events WHERE event_id=%s',(unassigned,))['id']
    fact=distribution_trigger(aid,assigned)
    assert fact and fact['event_name']=='PageView' and fact['task_id']==str(point['task'])
    assert distribution_trigger(aid,assigned)==fact
    assert distribution_trigger(aid,unknown) is None
