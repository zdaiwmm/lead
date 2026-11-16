"""Natural distribution feedback uses the existing bounded decision lifecycle."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from lead_app import worker
from lead_app.db import one, tx, uid
from lead_app.growth import outcomes, tools
from lead_app.growth.contracts import ExperimentSpec, TaskSpec, utcnow
from lead_app.growth.runtime import advance, consume_triggers, ensure_decision, finish
from lead_app.growth.store import create_experiment, submit_work
from lead_app.workflows import GrowthDecisionWorkflow
from temporalio.common import WorkflowIDConflictPolicy
from test_growth_runtime import new_work


def distribution_fact():
    return {"reason": "distribution_signal", "event_name": "ShortLinkClick",
            "account_id": uid(), "experiment_id": uid(), "task_id": uid(), "ingest_id": uid()}


@pytest.fixture
def delivery(monkeypatch):
    fact = distribution_fact()
    event = {"id": uid(), "agent_id": uid(), "lease_token": uid(), "occurred_at": utcnow(),
             "event_type": "analytics.cleaned", "payload": {"scope_key": "live", "ingest_id": fact["ingest_id"]}}
    state = SimpleNamespace(event=event, fact=fact, desired="running", queried=[], starts=[], acked=[], deferred=[], fail=False)

    def trigger(aid, ingest_id):
        state.queried.append((aid, ingest_id))
        return state.fact

    async def run_db(fn, *args):
        if fn.__name__ == "reconcile_source_config":
            return None
        if fn is worker.outbox_rows:
            return [state.event]
        if fn is worker.outbox_agent:
            return {"desired_state": state.desired}
        if fn is trigger:
            return fn(*args)
        if fn is worker.ack_outbox:
            state.acked.append(args)
            return None
        if fn is worker.defer_outbox:
            state.deferred.append(args)
            return None
        raise AssertionError(fn.__name__)

    async def start(*args, **kwargs):
        state.starts.append((args, kwargs))
        if state.fail:
            raise TimeoutError("ambiguous signal result")

    monkeypatch.setattr(outcomes, "distribution_trigger", trigger)
    monkeypatch.setattr(worker, "run_db", run_db)
    state.client = SimpleNamespace(start_workflow=start)
    return state


async def test_cleaned_live_business_fact_uses_original_decision_workflow(delivery):
    await worker.deliver_workspace(delivery.client, uid(), False, True)
    aid = delivery.event["agent_id"]
    assert delivery.queried == [(aid, delivery.fact["ingest_id"])]
    assert delivery.starts == [((GrowthDecisionWorkflow.run, aid), {
        "id": "growth-decision:" + aid, "task_queue": "lead",
        "id_conflict_policy": WorkflowIDConflictPolicy.USE_EXISTING,
        "start_signal": "wake", "start_signal_args": [delivery.fact]})]
    assert delivery.acked == [(aid, delivery.event["id"], delivery.event["lease_token"])]
    assert not delivery.deferred


@pytest.mark.parametrize("scope,desired", [("test", "running"), (None, "running"), ("live", "paused"),
                                          ("live", "draft"), ("live", "archived")])
async def test_nonlive_or_stopped_cleaned_event_does_not_wake(delivery, scope, desired):
    delivery.event["payload"]["scope_key"] = scope
    delivery.desired = desired
    await worker.deliver_workspace(delivery.client, uid(), False, True)
    assert not delivery.queried and not delivery.starts and not delivery.deferred
    assert len(delivery.acked) == 1


async def test_unqualified_cleaned_receipt_is_acknowledged_without_model_wake(delivery):
    delivery.fact = None
    await worker.deliver_workspace(delivery.client, uid(), False, True)
    assert len(delivery.queried) == len(delivery.acked) == 1
    assert not delivery.starts and not delivery.deferred


async def test_unknown_workflow_rpc_retries_only_same_durable_fact(delivery):
    delivery.fail = True
    await worker.deliver_workspace(delivery.client, uid(), False, True)
    assert not delivery.acked and len(delivery.deferred) == 1
    delivery.fail = False
    await worker.deliver_workspace(delivery.client, uid(), False, True)
    assert delivery.starts[0] == delivery.starts[1]
    assert len(delivery.acked) == 1


def test_distribution_ingest_replay_and_burst_share_bounded_decision(growth_agent):
    new_work(growth_agent)
    first = distribution_fact()
    ref = ensure_decision(growth_agent, [first])
    assert ref and ensure_decision(growth_agent, [first]) == ref
    for _ in range(20):
        latest = {**first, "ingest_id": uid(), "task_id": uid()}
        assert ensure_decision(growth_agent, [latest]) == ref
    consume_triggers(growth_agent, [first])
    with tx(growth_agent) as c:
        runtime = one(c, "SELECT onboarding_state FROM lead.agents WHERE id=%s", (growth_agent,))["onboarding_state"]["growth_runtime"]
        assert len(runtime["pending_triggers"]) == 1
        pending = next(iter(runtime["pending_triggers"].values()))
        assert pending["ingest_id"] == latest["ingest_id"]
        c.execute("UPDATE lead.tasks SET state='succeeded',completed_at=now() WHERE id=%s", (ref["task_id"],))
    consume_triggers(growth_agent, [pending])
    assert ensure_decision(growth_agent, [latest]) is None
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.decision'")["n"] == 1


def test_distribution_fact_respects_existing_pause(growth_agent):
    new_work(growth_agent)
    fact = distribution_fact()
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s", (growth_agent,))
    assert ensure_decision(growth_agent, [fact]) is None
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.decision'")["n"] == 0
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (growth_agent,))
    assert ensure_decision(growth_agent)


def signal_summary(clicks=0, revision="signals-one", **extra):
    return {"new_units": 0, "evidence_revision": "unchanged-core-evidence", "signals_revision": revision,
            "shortlinks": {"business_clicks": clicks, "clicks": 20, "unattributed": 20, "pending": 4},
            "visits": {"scope": "project_context", "page_views": 100},
            "event_funnel": [{"count": 100}], **extra}


async def observe(aid, monkeypatch, summary, *, experiment=None, payload_experiment=None):
    expected = str(payload_experiment or experiment) if payload_experiment or experiment else None

    def summarize(actual_aid, actual_experiment):
        assert actual_aid == aid
        assert (str(actual_experiment) if actual_experiment else None) == expected
        return deepcopy(summary)

    monkeypatch.setattr(outcomes, "summarize", summarize)
    with tx(aid) as c:
        ref = submit_work(c, aid, TaskSpec(kind="observe", purpose="distribution", experiment_id=experiment,
            payload={"experiment_id": str(payload_experiment)} if payload_experiment else {},
            dedupe_key=uid(), review_at=utcnow() + timedelta(hours=1)))
    ready = advance(ref)
    assert ready["state"] == "ready"
    with tx(aid) as c:
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (ref["task_id"],))
    result = await tools.execute_tool(aid, row, ready["claim"])
    assert finish(ready["claim"], result)["accepted_control"]
    with tx(aid) as c:
        wake = one(c, """SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake'
            AND payload->>'reason'='evidence_added' AND payload->>'task_id'=%s""", (ref["task_id"],))["n"]
    assert wake == int(result.output["knowledge_progress"] or result.output["business_progress"])
    return result.output


async def test_empty_or_project_context_observations_do_not_mint_knowledge(growth_agent, monkeypatch):
    new_work(growth_agent)
    for value in (0, None, True, -1):
        result = await observe(growth_agent, monkeypatch, signal_summary(value))
        assert not result["knowledge_progress"] and not result["business_progress"]
    for revision in (None, ""):
        result = await observe(growth_agent, monkeypatch, signal_summary(1, revision))
        assert not result["knowledge_progress"] and not result["business_progress"]


async def test_observe_distinct_signals_progress_without_changing_core_value(growth_agent, monkeypatch):
    new_work(growth_agent)
    first = await observe(growth_agent, monkeypatch, signal_summary(1, new_units=1))
    assert first["knowledge_progress"] and first["business_progress"]
    repeat = await observe(growth_agent, monkeypatch, signal_summary(1, new_units=1))
    assert not repeat["knowledge_progress"] and not repeat["business_progress"]
    changed = await observe(growth_agent, monkeypatch, signal_summary(2, "signals-two", new_units=1))
    assert changed["knowledge_progress"] and not changed["business_progress"]
    assert changed["new_units"] == 1


async def test_observe_signal_dedup_uses_effective_experiment_scope(growth_agent, monkeypatch):
    new_work(growth_agent)
    spec = ExperimentSpec(question_type="distribution", question="Which bounded point is observed?",
        hypothesis="A real distribution point reaches a visitor", learning_signal="An attributed business click",
        token_limit=1000, review_at=utcnow() + timedelta(hours=1))
    with tx(growth_agent) as c:
        first = create_experiment(c, growth_agent, spec)["id"]
        second = create_experiment(c, growth_agent, spec.model_copy(update={"variation": "second"}))["id"]
    summary = signal_summary(1)
    assert (await observe(growth_agent, monkeypatch, summary, experiment=first))["knowledge_progress"]
    assert (await observe(growth_agent, monkeypatch, summary, experiment=second))["knowledge_progress"]
    assert not (await observe(growth_agent, monkeypatch, summary, experiment=second,
                              payload_experiment=first))["knowledge_progress"]
    assert (await observe(growth_agent, monkeypatch, summary))["knowledge_progress"]
    assert not (await observe(growth_agent, monkeypatch, summary))["knowledge_progress"]
