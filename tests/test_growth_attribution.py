from datetime import timedelta

import pytest
from lead_app.db import j, one, tx, uid
from lead_app.growth.contracts import ExperimentSpec, utcnow
from lead_app.growth.outcomes import classify_receipts, summarize
from lead_app.growth.store import create_experiment
from lead_app.pixel_events import PixelEvent
from pydantic import ValidationError
from test_pixel_events import pixel  # noqa: F401


def test_first_value_is_typed_and_excludes_non_customer_proofs():
    with pytest.raises(ValidationError):
        PixelEvent(event_id="x",event_name="Activation",properties={"first_business_value":"yes"})
    rows = [{"event_id":"one","counted":True,"trust_level":"server_verified","unit_key":"u",
        "first_business_value":True,"subject_type":"user","audience_class":"team","channel":"facebook"}]
    assert classify_receipts(rows)["new_units"] == 0
    assert classify_receipts(rows)["excluded"] == 1


def test_cross_revision_history_prevents_relabelled_new_customer():
    now = utcnow()
    common = {"counted":True,"trust_level":"server_verified","unit_key":"u","first_business_value":True}
    rows = [{**common,"event_id":"old","definition_revision":1,"occurred_at":now-timedelta(days=10)},
        {**common,"event_id":"new","definition_revision":2,"occurred_at":now,"channel":"x"}]
    result = classify_receipts(rows,start=now-timedelta(days=1),definition_revision=2)
    assert result["new_units"] == 0
    assert result["repeat_units"] == 1


def test_experiment_freezes_definition_and_missing_is_not_live_fallback(request):
    aid,cid,client,headers,_,_ = request.getfixturevalue("pixel")
    spec = ExperimentSpec(question_type="conversion",question="Does the core value work?",
        hypothesis="A visitor can obtain core value",learning_signal="Trusted first value",
        token_limit=1000,review_at=utcnow()+timedelta(hours=1))
    with tx(aid) as c:
        empty = create_experiment(c,aid,spec)
        definition = one(c,"SELECT revision FROM lead.pixel_definitions WHERE connection_id=%s AND name='Activation'",(cid,))
        goal = {"connection_id":cid,"event_name":"Activation","definition_revision":definition["revision"],
            "unit":"pair","attribution_window_days":7}
        c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",(j({"operating_contract":{"revision":2,"event_ref":goal}}),aid))
        frozen = create_experiment(c,aid,spec.model_copy(update={"question_type":"value"}))
    assert summarize(aid,empty["id"])["measurement"]=="missing"
    assert frozen["specification"]["activation_ref"]["unit"]=="pair"
    assert frozen["specification"]["activation_ref"]["attribution_window_days"]==7
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{operating_contract,event_ref,unit}','\"user\"') WHERE id=%s",(aid,))
    assert summarize(aid,frozen["id"])["definition_ref"]["unit"]=="pair"


def test_pair_business_dedup_uses_private_opaque_subject(request):
    aid,cid,client,headers,_,key = request.getfixturevalue("pixel")
    event = client.post(f"/api/agents/{aid}/analytics/events/configure",headers=headers,json={
        "connection_id":cid,"name":"MutualValue","label":"双向成功","method":"server","definition":"双方完成一次成功交互"})
    assert event.status_code==200,event.text
    body = {"connection_id":cid,"event_id":uid(),"event_name":"MutualValue","conversion_id":"opaque-pair-123",
        "properties":{"subject_type":"pair","first_business_value":True,"rule_version":"MutualValue-v1"}}
    auth = {"Authorization":"Bearer "+key}
    first = client.post("/collect/server/events",headers=auth,json=body)
    again = client.post("/collect/server/events",headers=auth,json={**body,"event_id":uid()})
    assert first.status_code==200,first.text
    assert again.status_code==200 and again.json()["duplicate"]
    with tx(aid) as c:
        row = one(c,"SELECT business_key FROM lead.pixel_receipts WHERE event_name='MutualValue' AND counted")
        assert row["business_key"] and "opaque-pair-123" not in row["business_key"]


def test_decision_context_uses_actual_visit_funnel_and_event_specific_freshness(request):
    from lead_app.growth.agent import build_context
    from test_pixel_events import send
    source=request.getfixturevalue('pixel')
    aid,cid,client,headers,_,key=source
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",(
            j({'operating_contract':{'revision':1,'event_ref':{'connection_id':cid,'event_name':'Activation','definition_revision':1}}}),aid))
    baseline=client.post('/collect/server/events',headers={'Authorization':'Bearer '+key},json={
        'connection_id':cid,'event_name':'Activation','event_id':uid(),'user_id':'synthetic-existing-user',
        'properties':{'first_business_value':False,'rule_version':'activation-v1'}})
    assert baseline.status_code==200,baseline.text
    before=build_context(aid)['outcomes']
    for _ in range(5):
        assert send(source).status_code==200
    after=build_context(aid)['outcomes']
    assert before['visits']['visitors']==0 and after['visits']['visitors']==5
    assert after['new_units']==0 and after['measurement']=='healthy'
    assert after['exposure']['value'] is None and after['costs']['model_tokens']==0
    import psycopg
    from lead_app.config import database_kwargs
    with psycopg.connect(**database_kwargs(owner=True)) as c:
        c.execute("UPDATE lead.pixel_receipts SET received_at=now()-interval '10 days' WHERE connection_id=%s AND event_name='Activation'",(cid,))
    stale=build_context(aid)['outcomes']
    assert stale['measurement']=='stale' and stale['visits']['coverage']=='healthy'


async def test_async_pair_uses_frozen_source_without_browser_identity_and_ignores_late_earlier_visit(request,monkeypatch,process_pending_clicks):
    from urllib.parse import parse_qs, urlsplit

    from lead_app import analytics
    from test_growth_unknown_receipts import facebook_work, real_submit
    from test_pixel_events import send
    monkeypatch.setattr(analytics,'public_origin',lambda:'https://lead.example')
    source=request.getfixturevalue('pixel')
    aid,cid,client,headers,markers,key=source
    definition=client.post(f'/api/agents/{aid}/analytics/events/configure',headers=headers,json={
        'connection_id':cid,'name':'MutualValue','label':'双向价值','method':'server','definition':'双方完成核心行为'})
    assert definition.status_code==200,definition.text
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",(
            j({'operating_contract':{'revision':1,'event_ref':{'connection_id':cid,'event_name':'MutualValue','definition_revision':1,'unit':'pair'}}}),aid))
    with facebook_work(aid,'https://site.example/') as (ref,data,account):
        await real_submit(aid,monkeypatch,ref,data,account)
        with tx(aid) as c:
            link=one(c,'SELECT l.* FROM lead.actions a JOIN lead.tracking_links l ON l.content_id=a.content_id WHERE a.task_id=%s',(ref['task_id'],))
        anonymous=uid()
        redirect=client.get('/r/'+link['public_code'],follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'})
        proof=parse_qs(urlsplit(redirect.headers['location']).query)['lc'][0]
        arrival=send(source,anonymous_id=anonymous,ref=link['public_code'],click_token=proof)
        frozen=arrival.json()['attribution']
        assert frozen['touchpoint_id']
        # Arrives after the source was frozen, although its event clock claims
        # it happened earlier. It must not rewrite the transaction's source.
        assert send(source,anonymous_id=anonymous,ref=markers[1],occurred_at=(utcnow()-timedelta(minutes=5)).isoformat()).status_code==200
        response=client.post('/collect/server/events',headers={'Authorization':'Bearer '+key},json={
            'connection_id':cid,'event_name':'MutualValue','event_id':uid(),'conversion_id':'synthetic-opaque-pair',
            'touchpoint_id':frozen['touchpoint_id'],'click_id':frozen['click_id'],
            'properties':{'subject_type':'pair','first_business_value':True,'value_qualified':True,'rule_version':'MutualValue-v1'}})
        assert response.status_code==200,response.text
        process_pending_clicks(aid)
        result=summarize(aid)
        assert result['unit']=='pair' and result['new_units']==1 and result['channels']=={'facebook':1}
        assert 'synthetic-opaque-pair' not in str(result)
