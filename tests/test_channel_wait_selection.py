"""Actual persisted clock waits preserve independent target constraints."""
# ruff: noqa: F811

from datetime import datetime, timedelta, timezone

import pytest
from lead_app import channel_delivery, channel_research, operating_kernel
from lead_app.db import j, one, tx, uid
from test_channel_authority import authorize, managed_pair  # noqa: F401


def setup_clock(pair, monkeypatch, deadlines, evidence):
    aid, account, _ = pair
    authorize(pair)
    targets = [dict(target_id=key, platform="facebook", account_id=account["id"])
               for key in deadlines]
    monkeypatch.setattr(operating_kernel, "eligible_publish_targets", lambda *a: targets)
    monkeypatch.setattr(channel_delivery, "opportunity", lambda c, key: evidence.get(key))
    with tx(aid) as c:
        clock = one(c, "SELECT * FROM lead.tasks WHERE type='channel.clock' AND input->>'lane'='delivery'")
        c.execute("UPDATE lead.tasks SET result=%s WHERE id=%s", (j({"target_deadlines": deadlines}), clock["id"]))
    return dict(agent_id=aid, task_id=str(clock["id"]), generation=clock["version"], tick=uid())


def test_future_target_uses_absolute_deadline_not_poll(managed_pair, monkeypatch):
    key = uid()
    deadline = datetime.now(timezone.utc) + timedelta(hours=2)
    args = setup_clock(managed_pair, monkeypatch, {key: deadline.isoformat()}, {key: {"validated": True}})
    result = channel_delivery.next_target(args)
    decision = result["execution_cadence"]
    assert decision["state"] == "waiting_time"
    assert datetime.fromisoformat(decision["next_at"]) == deadline
    again = channel_delivery.next_target(args)
    assert again["execution_cadence"] == decision
    with tx(args["agent_id"]) as c:
        assert one(c, "SELECT scheduled_at FROM lead.tasks WHERE id=%s", (args["task_id"],))["scheduled_at"] == deadline


def test_one_target_gap_does_not_park_another_target_deadline(managed_pair, monkeypatch):
    missing, future = uid(), uid()
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    args = setup_clock(managed_pair, monkeypatch, {missing: "", future: deadline.isoformat()}, {future: {"validated": True}})
    result = channel_delivery.next_target(args)
    assert result["execution_cadence"]["state"] == "waiting_time"
    with tx(args["agent_id"]) as c:
        saved = one(c, "SELECT result FROM lead.tasks WHERE id=%s", (args["task_id"],))["result"]
        assert saved["waiting_targets"][missing]["state"] == "waiting_event"
        assert saved["waiting_targets"][future]["next_at"] == result["execution_cadence"]["next_at"]


def test_same_target_retains_time_and_missing_opportunity(managed_pair, monkeypatch):
    key = uid()
    args = setup_clock(managed_pair, monkeypatch, {key: (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}, {})
    result = channel_delivery.next_target(args)
    decision = result["execution_cadence"]
    assert decision["state"] == "waiting_event" and decision["next_at"] is None
    assert len(decision["constraints"]) == 2


def test_deadline_order_uses_instants_with_offsets(managed_pair, monkeypatch):
    early, late = uid(), uid()
    now = datetime.now(timezone.utc)
    deadlines = {early: (now+timedelta(hours=1)).astimezone(timezone(timedelta(hours=8))).isoformat(),
                 late: (now+timedelta(hours=2)).isoformat()}
    result = channel_delivery.next_target(setup_clock(managed_pair, monkeypatch, deadlines, {early: {"ok": True}, late: {"ok": True}}))
    assert datetime.fromisoformat(result["execution_cadence"]["next_at"]) == datetime.fromisoformat(deadlines[early])


@pytest.mark.parametrize("bad", ["not-a-date", "2027-01-01T00:00:00"])
def test_bad_target_deadline_does_not_block_independent_ready_target(managed_pair, monkeypatch, bad):
    invalid, ready = uid(), uid()
    args = setup_clock(managed_pair, monkeypatch, {invalid: bad, ready: ""}, {invalid: {"ok": True}, ready: {"ok": True}})
    assert channel_delivery.next_target(args)["target_id"] == ready


def test_source_and_business_gaps_both_remain_visible(managed_pair, monkeypatch):
    aid, *_ = managed_pair
    authorize(managed_pair)
    monkeypatch.setenv("LEAD_APPROVED_DISCOVERY_CHANNELS", "")
    with tx(aid) as c:
        c.execute("INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description) SELECT tenant_id,id,2,'' FROM lead.agents WHERE id=%s", (aid,))
        clock = one(c, "SELECT * FROM lead.tasks WHERE type='channel.clock' AND input->>'lane'='research'")
    result, work = channel_research.select(dict(agent_id=aid, task_id=str(clock["id"]), generation=clock["version"], tick=uid()))
    assert work is None
    assert {item["reason_code"] for item in result["execution_cadence"]["constraints"]} == {"source_permission_required", "business_required"}
