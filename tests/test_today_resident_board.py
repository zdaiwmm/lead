from datetime import datetime, timedelta, timezone
from uuid import UUID

from lead_app import domain, operating_plans
from lead_app.db import j, tx, uid
from lead_app.scope import current_tenant
from lead_app.telegram_group_board import resident_activity


def test_today_reports_search_while_account_writes_are_held():
    aid = domain.create_agent()
    domain.update_business(aid, 1, "https://example.test", "Reading group", "readers")
    now = datetime.now(timezone.utc)
    before_today = now - timedelta(days=2)
    resident = {"mode": "production", "action": "waiting", "heartbeat_at": now.isoformat(),
                "native_search_after": (now + timedelta(minutes=25)).isoformat(),
                "account_status": "undetermined", "writes_enabled": False}
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running', onboarding_state=onboarding_state || %s WHERE id=%s",
                  (j({"community_growth": {"enabled": True}, "telegram_executor": resident}), aid))
        insert = """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,
            scheduled_at,completed_at) VALUES(%s,%s,'community.discover',%s,%s,%s,%s,now(),%s)"""
        candidate = {"name": "Test group", "telegram_url": "https://t.me/example"}
        for at in (before_today, now):
            c.execute(insert, (current_tenant(), aid, "succeeded", uid(), j({}), j({"candidates": [candidate]}), at))
        c.execute(insert, (current_tenant(), aid, "running", uid(), j({"native_queries": ["a", "b", "c"]}),
                           j({"phase": "native_collect", "native_cursor": 1, "sources": [{}],
                              "checkpoint": {"at": now.isoformat()}}), None))
    def board():
        return operating_plans.status(UUID(aid), today_only=True)["telegram_group_board"]

    first = board()
    assert "冷却" in first["current"] and "复查" in first["next"]
    assert "入群和发言已暂停" in first["resident"]["write_hold"]
    assert first["resident"]["discovery"]["native_cursor"] == 1
    assert "candidate" not in first["metrics"]
    assert first["autonomy"]["last_progress_at"] == before_today

    with tx(aid) as c:
        c.execute("""UPDATE lead.tasks SET result=result || %s WHERE agent_id=%s AND state='running'""",
                  (j({"native_cursor": 2, "sources": [{}, {}]}), aid))
    updated = board()
    assert updated["resident"]["discovery"]["native_cursor"] == 2
    assert updated["resident"]["discovery"]["query_count"] == 3
    assert updated["resident"]["discovery"]["source_count"] == 2
    assert updated["autonomy"] == first["autonomy"]


def test_pause_and_missing_or_expired_heartbeat():
    now = datetime.now(timezone.utc)
    agent = {"desired_state": "running", "onboarding_state": {}}
    assert resident_activity(agent, [], now) is None
    state = {"mode": "production", "heartbeat_at": (now - timedelta(minutes=5)).isoformat(),
             "action": "discover", "writes_enabled": True, "next_check_at": now.isoformat()}
    agent["onboarding_state"]["telegram_executor"] = state
    for value in (state["heartbeat_at"], None, "bad-date"):
        state["heartbeat_at"] = value
        view = resident_activity(agent, [], now)
        assert "心跳未更新" in view["current"]
        assert view["next_check_at"] is None
    agent["desired_state"] = "paused"
    assert "暂停" in resident_activity(agent, [], now)["current"]
