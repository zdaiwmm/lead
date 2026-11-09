from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from lead_app import community_discovery as discovery
from lead_app import community_growth as growth
from lead_app import community_runtime as runtime
from lead_app import hybrid_research, model, telegram_group_board
from lead_app.db import j, one, require_agent, tx, uid
from lead_app.scope import current_tenant, tenant_scope
from test_telegram_delivery_and_fairness import create_task
from test_telegram_delivery_and_fairness import workspace as workspace


@pytest.mark.asyncio
async def test_untrusted_checkpoints_cannot_reach_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("protected community payload reached provider configuration")
    monkeypatch.setattr(model, "model_config", forbidden)
    sentinel = "UNAUTHORIZED-TELEGRAM-SENTINEL"
    prompt = {"community":{"history":[sentinel]}, "actual_outcomes":[sentinel], "entries":[sentinel],
              "observed_links":[], "previous_queries":[sentinel], "recent_posts":[sentinel], "business":{}}
    for cls in (discovery.QueryPlan, discovery.CatalogSelection, growth.Discovery, runtime.CommunityAdmission,
                runtime.ObservationAdmission, runtime.CommunityDecision, runtime.PlacementDecision, runtime.ReplyRevision):
        result = await model.run_model(cls, prompt, agent_id=runtime.CHAMBER_AGENT_ID)
        assert sentinel not in result.model_dump_json()
    with pytest.raises(ValueError, match="not_authorized"):
        await model.run_model(str,prompt,agent_id=runtime.CHAMBER_AGENT_ID)
    assert (await hybrid_research.explore(runtime.CHAMBER_AGENT_ID, "https://t.me/example", sentinel,
                                         mode="community_discovery"))["sources"] == []
    assert not discovery.hybrid_pending({"chamber_campaign":True}, {})


def test_chamber_cadence_uses_config_not_join_shortfall():
    a={"onboarding_state":{"growth_trial_policy":{"mode":"active","focus":"chamber_first_users"}}}
    assert growth.search_interval(None,a,SimpleNamespace(discovery_interval_hours=2))==2


def test_scoped_review_idempotence_revocation_and_aggregate(workspace,monkeypatch):
    aid,base=workspace
    monkeypatch.setattr(runtime,"CHAMBER_AGENT_ID",aid)
    now=datetime.now(timezone.utc)
    with tx(aid) as c:
        t=create_task(c,workspace,"-701","succeeded",{"join_state":"confirmed","membership_origin":"existing"})
        observed={"target_id":"-701","kind":"discussion","joined":True,"can_reply":True,"title":"test",
                  "description":"Members may showcase their projects here.","history":[{"mid":"15","own":False,"body":"Any example?"}]}
        runtime.offer_community_review(c,aid,t,observed)
        runtime.offer_community_review(c,aid,t,observed)
        cards=c.execute("SELECT id,payload FROM lead.interaction_cards WHERE type='community_operator_review'").fetchall()
        assert len(cards)==2 and all('Any example?' not in str(v) for v in cards)
        reply=next(v for v in cards if v["payload"]["kind"]=="reply")
        cid=str(reply["id"])
        operator={"mode":"production","account_status":"no_limits","writes_enabled":True,"heartbeat_at":now.isoformat()}
        c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state||%s WHERE id=%s",(j({"telegram_executor":operator}),aid))
    with tenant_scope(current_tenant(),{"role":"owner","issuer":"local"}):
        made=runtime.approve_community_review(aid,cid,accept=True,body="Here is an example.",permission="Group permits this reply.")
        assert made==runtime.approve_community_review(aid,cid,accept=True,body="Here is an example.",permission="Group permits this reply.")
        with tx(aid) as c:
            task=one(c,"SELECT * FROM lead.tasks WHERE id=%s",(made["task_id"],))
            assert runtime.approved_operation(c,task,"-701","reply")
            assert not runtime.approved_operation(c,task,"-702","reply")
            assert not runtime.approved_operation(c,task,"-701","join")
            c.execute("UPDATE lead.interaction_cards SET expires_at=now()-interval '1 second' WHERE id=%s",(cid,))
            assert not runtime.approved_operation(c,task,"-701","reply")
            c.execute("UPDATE lead.interaction_cards SET expires_at=now()+interval '1 minute' WHERE id=%s",(cid,))
            board=telegram_group_board.report(c,require_agent(c,aid),now-timedelta(hours=1),now+timedelta(minutes=1))
            assert board["acquisition"]["reviewed_groups"]==1
        runtime.approve_community_review(aid,cid,accept=False)
        with tx(aid) as c:
            assert not runtime.approved_operation(c,task,"-701","reply")


def test_duplicate_candidate_aliases_count_once(workspace):
    aid,_=workspace
    now=datetime.now(timezone.utc)
    with tx(aid) as c:
        for url in ("https://t.me/oldgroup","https://t.me/newgroup"):
            bound=create_task(c,workspace,"-702","succeeded",{})
            c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{candidate,telegram_url}',%s) WHERE id=%s",(j(url),bound["id"]))
            c.execute("""INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,completed_at)
              VALUES(%s,%s,'community.discover','succeeded',%s,'{}',%s,now(),now())""",
              (current_tenant(),aid,uid(),j({"candidates":[{"telegram_url":url,"name":"test"}]})))
        b=telegram_group_board.report(c,require_agent(c,aid),now-timedelta(hours=1),now+timedelta(minutes=1))
        assert b["metrics"]["candidate"]["count"]==1 and b["acquisition"]["batches"]==2


@pytest.mark.parametrize("text,zone,weekday,expected",[
    ("Members may showcase projects on Fridays only.","Asia/Shanghai",4,True),
    ("Members may showcase projects on Fridays only.","Asia/Shanghai",3,False),
    ("Members may showcase projects on Fridays only.","",4,False),
    ("Members may showcase projects only in #ads.","UTC",4,False),
    ("No advertisements.","UTC",4,False),
    ("Members may showcase projects.","",4,True),
])
def test_operator_post_permission_scope(text,zone,weekday,expected):
    at=datetime(2026,11,9+weekday,4,tzinfo=timezone.utc)
    assert runtime.review_rule_allows(text,zone,at) is expected


@pytest.mark.asyncio
async def test_local_discovery_accepts_bound_card_without_inferred_posting_permission(monkeypatch):
    monkeypatch.setattr(model, "model_config", lambda: pytest.fail("unexpected external provider"))
    link="https://t.me/chamber_fixture"
    observed={"telegram_url":link,"source_url":link,"name":"匿名树洞","subtitle":"500 members","page_excerpt":"500 members"}
    result=await model.run_model(growth.Discovery,{"observed_links":[observed]},agent_id=runtime.CHAMBER_AGENT_ID)
    assert [c.telegram_url for c in result.candidates]==[link]
    assert not result.candidates[0].rules_quote
