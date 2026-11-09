"""Regressions for the opt-in public observation and analytics read batch."""

import asyncio
import threading

import pytest
from lead_app import (
    analytics_delivery,
    analytics_reporting,
    budget,
    domain,
    operating_kernel,
    operating_work,
)
from lead_app.analytics_ingest import scoped_tx
from lead_app.db import one, require_agent, tx
from lead_app.scope import current_tenant


async def test_free_observation_precedes_model_gate(monkeypatch):
    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_PARALLEL_READ_AGENT_IDS", str(aid))
    calls = []

    async def read(self, operation, args):
        calls.append(operation)
        return {"url":args["url"], "title":"Fixture", "text":"Public product information collected without a model."}

    async def finish(self, state):
        assert state == "succeeded"
        return [{"url":"https://product.example/", "title":"Fixture"}]

    async def forbid(*args, **kwargs):
        pytest.fail("model called in public observation")

    monkeypatch.setattr(operating_work.BrowserTools, "command", read)
    monkeypatch.setattr(operating_work.BrowserTools, "finish", finish)
    monkeypatch.setattr(operating_work, "run_model", forbid)
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        budget.apply_daily_limit(c, aid, 0)
        business = one(c, """INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url)
            VALUES(%s,%s,1,'fixture','https://product.example/') RETURNING *""", (current_tenant(),aid))
        agent = require_agent(c, aid, True)
        choices, _, _ = operating_kernel.candidates(c, aid, agent, business)
        job = next(choice for choice in choices if choice.kind == "operating.journey")
        assert job.read_source and job.estimated_token_units == 0
    result = await operating_kernel.run_work(aid, job)
    assert result["reason"] == "public_observation_ready"
    with tx(aid) as c:
        choices, _, feedback = operating_kernel.candidates(c, aid, require_agent(c, aid, True), business)
        assert all(choice.key != job.key for choice in choices)
        assert any(item["key"] == job.key for item in feedback["scheduling"]["deferred"])
        row = one(c, "SELECT state,result FROM lead.tasks WHERE id=%s", (job.task_id,))
        assert row["state"] == "planned" and row["result"]["attempts"] == 0
        assert row["result"]["observation_checkpoint"]["observations"][0]["url"] == "https://product.example/"
        assert calls == ["open"] and budget.available(c, aid)["available_units"] == 0


async def test_dirty_day_finishes_before_blocked_delivery(monkeypatch):
    aid, tenant = domain.create_agent(), current_tenant()
    monkeypatch.setenv("LEAD_PARALLEL_READ_AGENT_IDS", str(aid))
    with scoped_tx(tenant, aid, "live") as c:
        c.execute("""INSERT INTO lead.analytics_report_dirty(tenant_id,agent_id,scope_key,day)
                     VALUES(%s,%s,'live',current_date-1)""", (tenant, aid))
    refreshed = threading.Event()
    refresh = analytics_reporting.refresh_scope

    def refresh_day(*args):
        days = refresh(*args)
        refreshed.set()
        return days

    async def slow(client, tenant, aid, scope):
        assert await asyncio.to_thread(refreshed.wait, 5)
        return 0

    monkeypatch.setattr(analytics_delivery, "deliver_scope", slow)
    monkeypatch.setattr(analytics_reporting, "refresh_scope", refresh_day)
    assert await analytics_delivery.deliver_partition(None, tenant, aid, "live") == 1
    assert await analytics_delivery.deliver_partition(None, tenant, aid, "live") == 0
    with scoped_tx(tenant, aid, "live") as c:
        assert one(c, "SELECT count(*) AS n FROM lead.analytics_report_days")["n"] == 1


def test_oldest_ready_dirty_day_progresses_despite_new_arrivals():
    aid, tenant = domain.create_agent(), current_tenant()
    with scoped_tx(tenant, aid, "live") as c:
        c.execute("""INSERT INTO lead.analytics_report_dirty(tenant_id,agent_id,scope_key,day,next_attempt_at)
            SELECT %s,%s,'live',current_date-n,now()-n*interval '1 hour' FROM generate_series(1,4) n""",
            (tenant, aid))
        oldest = one(c, "SELECT current_date-4 AS day")["day"]
    assert analytics_reporting.refresh_scope(tenant, aid, "live", limit=1) == [oldest.isoformat()]
