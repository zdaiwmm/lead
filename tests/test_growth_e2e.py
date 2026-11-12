"""Real local browser publication and API receipts; no external social writes."""
from datetime import timedelta

from lead_app.db import j, one, tx, uid
from lead_app.growth.contracts import ExperimentSpec, utcnow
from lead_app.growth.outcomes import summarize
from lead_app.growth.store import create_experiment
from test_growth_unknown_receipts import facebook_work, real_submit
from test_pixel_events import pixel, send  # noqa: F401


async def test_publication_entry_first_value_and_late_report_remain_distinct(request,monkeypatch,process_pending_clicks):
    from urllib.parse import parse_qs, urlsplit

    from lead_app import analytics
    monkeypatch.setattr(analytics,"public_origin",lambda:"https://lead.example")
    source_fixture = request.getfixturevalue("pixel")
    aid,cid,client,headers,_,key = source_fixture
    with facebook_work(aid,"https://site.example/") as (ref,data,account):
        with tx(aid) as c:
            definition = one(c,"SELECT revision FROM lead.pixel_definitions WHERE connection_id=%s AND name='Activation'",(cid,))
            c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",(
                j({"operating_contract":{"revision":1,"event_ref":{"connection_id":cid,"event_name":"Activation","definition_revision":definition["revision"]}}}),aid))
            exp = create_experiment(c,aid,ExperimentSpec(question_type="conversion",question="Can a visitor obtain core value?",
                hypothesis="The entry leads to actual product value",learning_signal="A trusted first core-value receipt",
                token_limit=10000,review_at=utcnow()+timedelta(hours=1)))
            c.execute("UPDATE lead.tasks SET experiment_id=%s,input=jsonb_set(input,'{spec,experiment_id}',to_jsonb(%s::text)) WHERE id=%s",(exp["id"],str(exp["id"]),ref["task_id"]))
        await real_submit(aid,monkeypatch,ref,data,account)
        with tx(aid) as c:
            action = one(c,"SELECT * FROM lead.actions WHERE task_id=%s",(ref["task_id"],))
            link = one(c,"SELECT * FROM lead.tracking_links WHERE content_id=%s",(action["content_id"],))
            final = one(c,"SELECT * FROM lead.content_items WHERE id=%s",(action["content_id"],))
            assert final["validation_result"]["source_content_id"]==str(data.content_id)
            assert link["platform"]=="facebook" and link["public_code"] in final["body"]["body"]
        assert summarize(aid,exp["id"])["new_units"] is None
        anonymous = uid()
        redirect = client.get("/r/"+link["public_code"],follow_redirects=False,headers={"User-Agent":"Mozilla/5.0"})
        click = parse_qs(urlsplit(redirect.headers["location"]).query)["lc"][0]
        assert send(source_fixture,anonymous_id=anonymous,ref=link["public_code"],click_token=click).status_code==200
        assert summarize(aid,exp["id"])["new_units"] is None
        auth = {"Authorization":"Bearer "+key}
        event = {"connection_id":cid,"event_name":"Activation","event_id":uid(),"anonymous_id":anonymous,
            "user_id":"synthetic-customer-one","occurred_at":utcnow().isoformat(),
            "properties":{"first_business_value":True,"value_qualified":True,"rule_version":"activation-v1"}}
        response = client.post("/collect/server/events",headers=auth,json=event)
        assert response.status_code==200,response.text
        process_pending_clicks(aid)
        first = summarize(aid,exp["id"])
        assert first["measurement"]=="healthy" and first["new_units"]==1
        assert first["channels"]=={"facebook":1}
        assert client.post("/collect/server/events",headers=auth,json=event).json()["duplicate"]
        assert summarize(aid,exp["id"])["new_units"]==1
        # A later receipt creates a new report revision; saved earlier evidence
        # remains inspectable and never reopens the original publication.
        other = uid()
        redirect = client.get("/r/"+link["public_code"],follow_redirects=False,headers={"User-Agent":"Mozilla/5.0"})
        click = parse_qs(urlsplit(redirect.headers["location"]).query)["lc"][0]
        assert send(source_fixture,anonymous_id=other,ref=link["public_code"],click_token=click).status_code==200
        late = {**event,"event_id":uid(),"anonymous_id":other,"user_id":"synthetic-customer-two","occurred_at":utcnow().isoformat()}
        assert client.post("/collect/server/events",headers=auth,json=late).status_code==200
        process_pending_clicks(aid)
        second = summarize(aid,exp["id"])
        assert second["new_units"]==2 and second["evidence_revision"]!=first["evidence_revision"]
        assert first["new_units"]==1
        with tx(aid) as c:
            assert one(c,"SELECT count(*) n FROM lead.actions WHERE task_id=%s",(ref["task_id"],))["n"]==1
