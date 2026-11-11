"""Read-only projections cannot invent outcomes, deadlines, or compatibility."""

from datetime import datetime, timezone

import pytest
from test_cadence_state import cadence_agent as cadence_agent
from test_channel_authority import managed_pair as managed_pair
from test_community_handoff import partial as partial


@pytest.mark.parametrize('state', ['running','reconciling'])
def test_api_execution_includes_live_confirmation_and_pending_stock(cadence_agent,state):
    from datetime import timedelta
    from uuid import UUID

    from lead_app import operating_plans
    from lead_app.db import j, tx
    aid,_,_,task=cadence_agent
    now=datetime.now(timezone.utc)
    with tx(aid) as c:
        c.execute("INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description) SELECT tenant_id,id,1,'Language learning' FROM lead.agents WHERE id=%s",(aid,))
        c.execute("UPDATE lead.tasks SET state=%s,completed_at=NULL,created_at=now()-interval '2 days',result=%s WHERE id=%s",(state,j({'join_submitted':True,'join_state':'confirmed','join_confirmed_at':now.isoformat(),'reply_state':'unknown'}),task['id']))
    data=operating_plans.status(UUID(aid))['telegram_group_board']['execution']
    assert data['facts']['membership_confirmed']==1
    assert data['facts']['pending_receipt']==1
    assert data['facts']['message_confirmed']==0
    with tx(aid) as c:
        c.execute("UPDATE lead.tasks SET result=result || %s WHERE id=%s",(j({'join_confirmed_at':(now-timedelta(days=1)).isoformat()}),task['id']))
    data=operating_plans.status(UUID(aid))['telegram_group_board']['execution']
    assert data['facts']['membership_confirmed']==0
    assert data['facts']['pending_receipt']==1


def test_account_waits_do_not_inherit_another_account_deadline(managed_pair):
    from datetime import timedelta

    from lead_app import domain
    from lead_app.channel_authority import authorize_managed_scope
    from lead_app.db import j, one, tx
    from lead_app.execution_cadence import Scope, WaitEvidence, decide
    aid,a,b=managed_pair
    for account in (a,b):
        authorize_managed_scope(aid,str(account['id']),account['binding_version'],1)
    now=datetime.now(timezone.utc)
    decisions=[]
    for account in (a,b):
        scope=Scope(agent_id=aid,account_id=str(account['id']),binding_version=account['binding_version'],target_id=str(account['id']),operation='publish')
        evidence=WaitEvidence(scope=scope,source='capability',reason_code='awaiting_validated_opportunity' if account==a else 'legacy_unverified_target_window',evidence_ref='fixture',observed_at=now,policy_version=1,**({'resume_event':'candidate_validated'} if account==a else {'not_before':now+timedelta(hours=1)}))
        decisions.append(decide([evidence],now,subject=scope).model_dump(mode='json'))
    with tx(aid) as c:
        clock=one(c,"SELECT id FROM lead.tasks WHERE type='channel.clock' AND input->>'lane'='delivery'")
        c.execute("UPDATE lead.tasks SET result=%s WHERE id=%s",(j({'execution_cadence':decisions[1],'waiting_targets':{'a':decisions[0],'b':decisions[1]}}),clock['id']))
    accounts={str(row['id']):row for row in domain.agent_detail(aid)['accounts']}
    def delivery(account):return next(row['execution_cadence'] for row in accounts[str(account['id'])]['channel_waits'] if row['lane']=='delivery')
    assert delivery(a)['state']=='waiting_event' and delivery(a)['next_at'] is None
    assert delivery(b)['state']=='waiting_time' and delivery(b)['next_at'] is not None


def test_join_fact_survives_skipped_reply():
    from lead_app.community_participation import outcome_facts

    facts = outcome_facts(
        {
            "state": "succeeded",
            "result": {
                "join_submitted": True,
                "join_state": "confirmed",
                "outcome": {"state": "skipped"},
            },
        }
    )
    assert facts["membership_confirmed"] is True
    assert facts["message_confirmed"] is False
    assert facts["skipped"] is True


def test_unknown_reply_does_not_count_confirmed():
    from lead_app.community_participation import outcome_facts

    for result in (
        {"reply_state": "unknown"},
        {"reply_state": "confirmed"},
        {"reply_state": "confirmed", "reply_external_id": ""},
        {"reply_state": "confirmed", "reply_external_id": "   "},
        {"outcome": {"state": "pending_verification"}},
    ):
        facts = outcome_facts({"result": result})
        assert not facts["message_confirmed"]
        assert facts["pending_receipt"]
    assert outcome_facts(
        {"result": {"reply_state": "confirmed", "reply_external_id": "peer:message"}}
    )["message_confirmed"]


def test_waiting_event_has_no_fabricated_countdown():
    from lead_app.cadence_views import project
    from lead_app.execution_cadence import Scope, WaitEvidence, decide

    now = datetime.now(timezone.utc)
    scope = Scope(agent_id="fixture", account_id="account")
    evidence = WaitEvidence(
        scope=scope,
        source="capability",
        reason_code="source_permission_required",
        evidence_ref="fixture:permission",
        observed_at=now,
        resume_event="source_config_changed",
        policy_version=1,
    )
    raw = decide([evidence], now, subject=scope).model_dump(mode="json")
    raw["next_at"] = (
        now.isoformat()
    )  # Even malformed legacy timers cannot override an event gap.
    view = project(raw)
    assert view["state"] == "waiting_event"
    assert view["next_at"] is None
    assert view["reason_label"] == "来源访问尚未获准"
    assert view["scope_label"] == "当前账号"
    assert view["compatible"] is None


def test_legacy_payload_displays_unverified():
    from lead_app.cadence_views import project

    view = project({"delay": 1800, "reason": "waiting"})
    assert view["state"] == "unverified"
    assert view["next_at"] is None
    assert view["policy_version"] is None and view["compatible"] is None
    assert "待核实" in view["reason_label"]


def test_view_failure_does_not_block_executor(monkeypatch):
    from lead_app import cadence_views

    def broken(*a, **k):
        raise ValueError("malformed read-only presentation")

    monkeypatch.setattr(cadence_views, "project", broken)
    view = cadence_views.safe_project({})
    assert view["state"] == "unverified"


def test_unknown_limit_source_is_labeled_unverified(cadence_agent):
    from datetime import timedelta

    from lead_app.cadence_state import participation_decision
    from lead_app.cadence_views import project
    from lead_app.db import tx
    from test_cadence_state import activate_default

    aid, account, *_ = cadence_agent
    activate_default(aid)
    now = datetime.now(timezone.utc)
    with tx(aid) as c:
        raw = participation_decision(
            c,
            aid,
            "reply",
            [("reply", now - timedelta(minutes=1))],
            now,
            account_id=str(account["id"]),
        ).model_dump(mode="json")
        assert "来源待核实" in project(raw)["reason_label"]


def test_partial_checkpoint_counts_one_candidate(partial):
    from lead_app.db import tx
    from lead_app.telegram_group_board import METRICS
    from test_community_handoff import publish

    aid, *_ = partial
    now = datetime.now(timezone.utc)
    args = {
        "aid": aid,
        "day": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "now": now,
    }
    with tx(aid) as c:
        before = next(
            row["total"]
            for row in c.execute(METRICS, args).fetchall()
            if row["metric"] == "candidate"
        )
    publish(partial)
    publish(partial)
    args["now"] = datetime.now(timezone.utc)
    with tx(aid) as c:
        after = next(
            row["total"]
            for row in c.execute(METRICS, args).fetchall()
            if row["metric"] == "candidate"
        )
        assert after == before + 1


def test_receipt_fact_overrides_an_earlier_ready_projection():
    from lead_app.cadence_views import for_task

    task = {
        "input": {"account_id": "account"},
        "result": {
            "reply_state": "unknown",
            "execution_cadence": {
                "state": "ready",
                "constraints": [],
                "resume_events": [],
                "next_at": None,
                "execution_cadence_protocol": 1,
            },
        },
    }
    view = for_task(task)
    assert view["state"] == "receipt_only"
    assert view["next_at"] is None and view["evidence_source"] == "原操作回执"
    assert "原操作" in view["reason_label"]


def test_channel_api_does_not_expose_poll_time_as_business_deadline(
    managed_pair, monkeypatch
):
    from uuid import UUID

    from lead_app import channel_research, operating_plans
    from lead_app.db import one, tx, uid
    from test_channel_authority import authorize

    aid, *_ = managed_pair
    authorize(managed_pair)
    monkeypatch.setenv("LEAD_APPROVED_DISCOVERY_CHANNELS", "")
    with tx(aid) as c:
        clock = one(
            c,
            "SELECT * FROM lead.tasks WHERE type='channel.clock' AND input->>'lane'='research'",
        )
    channel_research.select(
        {
            "agent_id": aid,
            "task_id": str(clock["id"]),
            "generation": clock["version"],
            "tick": uid(),
        }
    )
    plan = operating_plans.status(UUID(aid))
    view = next(row for row in plan["channel_clocks"] if row["lane"] == "research")
    assert view["next_at"] is None
    assert view["execution_cadence"]["state"] == "waiting_event"
    assert view["execution_cadence"]["reason_label"] == "来源访问尚未获准"


def test_account_api_reports_only_the_authorized_account(managed_pair):
    from lead_app import domain
    from test_channel_authority import authorize

    aid, a, b = managed_pair
    authorize(managed_pair)
    rows = {str(row["id"]): row for row in domain.agent_detail(aid)["accounts"]}
    assert rows[str(a["id"])]["managed_scope_authorized"] is True
    assert rows[str(b["id"])]["managed_scope_authorized"] is False
    assert rows[str(b["id"])]["automation_enabled"] is False


def test_reddit_member_account_exposes_its_actual_wait(managed_pair, monkeypatch):
    from lead_app import channel_research, domain, reddit_member
    from lead_app.db import one, tx, uid

    aid, account, *_ = managed_pair
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.social_accounts SET platform='reddit',automation_enabled=true WHERE id=%s",
            (account["id"],),
        )
        c.execute(
            "UPDATE lead.browser_profiles SET slot='reddit' WHERE id=%s",
            (account["profile_id"],),
        )
    reddit_member.authorize(aid, str(account["id"]), account["binding_version"])
    monkeypatch.setenv("LEAD_APPROVED_DISCOVERY_CHANNELS", "")
    with tx(aid) as c:
        clock = one(
            c,
            "SELECT * FROM lead.tasks WHERE type='channel.clock' AND input->>'lane'='research'",
        )
    channel_research.select(
        {
            "agent_id": aid,
            "task_id": str(clock["id"]),
            "generation": clock["version"],
            "tick": uid(),
        }
    )
    row = next(
        row
        for row in domain.agent_detail(aid)["accounts"]
        if row["id"] == account["id"]
    )
    assert row["managed_scope_authorized"] is False
    assert row["member_scope_authorized"] is True
    assert (
        next(w for w in row["channel_waits"] if w["lane"] == "research")[
            "execution_cadence"
        ]["state"]
        == "waiting_event"
    )
