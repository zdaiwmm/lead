from datetime import timedelta

from lead_app.db import all_rows, j, one, tx
from lead_app.growth import runtime
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.store import submit_work
from test_growth_runtime import new_work

SCOPE = {"provider": "browser", "operation": "read", "origin": "https://forum.example", "access_mode": "public"}


def blocked(aid):
    new_work(aid)
    with tx(aid) as c:
        ref = submit_work(c, aid, TaskSpec(kind="research", purpose="demand", payload={"urls": ["https://forum.example/a"], "queries": ["needs"]}, review_at=utcnow()+timedelta(hours=1)))
    for n in range(3):
        claim = runtime.advance(ref, now=utcnow()+timedelta(minutes=n))["claim"]
        runtime.finish(claim, WorkResult(outcome="failed", failure_code="source_unavailable", output={"failure_scope": SCOPE}))
    return ref


def test_actual_read_failure_does_not_poison_search(growth_agent):
    ref = blocked(growth_agent)
    with tx(growth_agent) as c:
        root = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (ref["task_id"],))
        other = submit_work(c, growth_agent, TaskSpec(kind="research", purpose="alternative", payload={"queries": ["another question"]}, review_at=utcnow()+timedelta(hours=1)))
    episode = root["result"]["runtime"]["recovery"]
    assert episode["resource"] != "research:public_search"
    assert episode["failure_scope"] == SCOPE
    assert runtime.advance(other)["state"] == "ready"


def test_actual_call_waits_and_deduplicates_blocked_notification(growth_agent):
    blocked(growth_agent)
    for n in range(5):
        with tx(growth_agent) as c:
            other = submit_work(c, growth_agent, TaskSpec(kind="research", purpose="dependent", dedupe_key=str(n), review_at=utcnow()+timedelta(hours=1)))
        assert runtime.check_research_resource(growth_agent, other["task_id"], SCOPE)
    with tx(growth_agent) as c:
        notices = all_rows(c, "SELECT payload FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='resource_blocked'")
        assert len(notices) == 1
        tasks = all_rows(c, "SELECT state,result FROM lead.tasks WHERE result#>'{runtime,blocked_resources}' IS NOT NULL")
        assert len(tasks) >= 5
        assert all(task["state"] != "failed" for task in tasks)


def dependent(aid, name, scope=SCOPE):
    with tx(aid) as c:
        ref = submit_work(c, aid, TaskSpec(kind="research", purpose="dependent", dedupe_key=name, review_at=utcnow()+timedelta(hours=1)))
    runtime.check_research_resource(aid, ref["task_id"], scope)
    return ref


def consume(aid, root_ref):
    with tx(aid) as c:
        root = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (root_ref["task_id"],))
        episode = root["result"]["runtime"]["recovery"]
        episode["probe_token"] = "test-lease"
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s", (j(episode), root_ref["task_id"]))
    return runtime._consume_recovery(aid, root_ref["task_id"], episode, "fixture:source-read", episode["condition_revision"], "test-lease")


def test_succeeded_root_resumes_all_current_dependencies_once(growth_agent):
    root = blocked(growth_agent)
    refs = [dependent(growth_agent, str(n)) for n in range(4)]
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET state='succeeded' WHERE id=%s", (root["task_id"],))
    resumed = consume(growth_agent, root)
    assert {item["task_id"] for item in resumed} == {item["task_id"] for item in refs}
    assert all(item["task_generation"] == 2 for item in resumed)
    assert consume(growth_agent, root) is None
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='resource_recovered'")["n"] == 1
        assert one(c, "SELECT input->>'task_generation' generation FROM lead.tasks WHERE id=%s", (root["task_id"],))["generation"] == '1'


def test_paused_expired_and_canceled_dependencies_do_not_resume(growth_agent):
    root = blocked(growth_agent)
    expired = dependent(growth_agent, "expired")
    canceled = dependent(growth_agent, "canceled")
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{spec,review_at}',%s) WHERE id=%s", (j((utcnow()-timedelta(seconds=1)).isoformat()), expired["task_id"]))
        c.execute("UPDATE lead.tasks SET state='canceled' WHERE id=%s", (canceled["task_id"],))
        c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s", (growth_agent,))
    assert consume(growth_agent, root) == []
    with tx(growth_agent) as c:
        assert not one(c, "SELECT id FROM lead.tasks WHERE (input->>'task_generation')::int>1")


def test_other_resource_wait_survives_first_proof(growth_agent):
    root = blocked(growth_agent)
    ref = dependent(growth_agent, "two-resources")
    with tx(growth_agent) as c:
        c.execute("""UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,blocked_resources}',
            result#>'{runtime,blocked_resources}' || %s) WHERE id=%s""",
            (j([{"episode_root":root["task_id"], "resource":"other-resource", "condition_revision":"other-condition"}]), ref["task_id"]))
    resumed = consume(growth_agent, root)
    assert ref["task_id"] not in {item["task_id"] for item in resumed}
    with tx(growth_agent) as c:
        row = one(c, "SELECT input,result FROM lead.tasks WHERE id=%s", (ref["task_id"],))
    assert row["input"]["task_generation"] == 1
    assert len(row["result"]["runtime"]["blocked_resources"]) == 1


def test_error_spelling_does_not_reset_resource_budget(growth_agent):
    root = blocked(growth_agent)
    ref = dependent(growth_agent, "renamed-error")
    with tx(growth_agent) as c:
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (ref["task_id"],))
        state = runtime._runtime(row)
        resource, condition = runtime.research_resource(SCOPE)
        state.update(resource_key=resource, condition_revision=condition, failure_scope=SCOPE)
        episode = runtime._record_failure(c, row, state, "different_transport_error")
    assert episode["root_task_id"] == root["task_id"]
    assert episode["attempts"] == 4


async def test_foreign_or_stale_probe_cannot_clear_original_failure(growth_agent, monkeypatch):
    from lead_app.growth import tools
    from lead_app.growth.tools import RecoverInput
    root = blocked(growth_agent)
    async def unrelated(*args):
        return WorkResult(outcome="completed", output={"recovery_checks":[{"resource":"other-resource", "condition_revision":"other-condition", "state":"ready", "evidence_ref":"fixture:other-site"}]})
    monkeypatch.setattr(tools, "probe_research_resource", unrelated, raising=False)
    with tx(growth_agent) as c:
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (root["task_id"],))
    data = RecoverInput(episode_root=root["task_id"], condition_revision=row["result"]["runtime"]["condition_revision"])
    result = await runtime.probe_episode(growth_agent, row, data)
    assert result.output["state"] == "still_blocked"
    with tx(growth_agent) as c:
        assert one(c, "SELECT result#>>'{runtime,recovery,status}' state FROM lead.tasks WHERE id=%s", (root["task_id"],))["state"] == 'blocked'


def test_competing_proofs_only_emit_one_generation(growth_agent):
    from concurrent.futures import ThreadPoolExecutor
    root = blocked(growth_agent)
    child = dependent(growth_agent, "concurrent")
    with tx(growth_agent) as c:
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (root["task_id"],))
        episode = row["result"]["runtime"]["recovery"]
        episode["probe_token"] = "concurrent-lease"
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s", (j(episode), root["task_id"]))
    def apply():
        return runtime._consume_recovery(growth_agent, root["task_id"], episode, "fixture:concurrent", episode["condition_revision"], "concurrent-lease")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert sum(item is not None for item in results) == 1
    with tx(growth_agent) as c:
        assert one(c, "SELECT input->>'task_generation' generation FROM lead.tasks WHERE id=%s", (child["task_id"],))["generation"] == '2'
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.work' AND payload->>'task_id'=%s AND payload->>'task_generation'='2'", (child["task_id"],))["n"] == 1


def test_probe_display_reports_observation_not_task_completion(growth_agent):
    from lead_app.growth.views import render_growth
    original = new_work(growth_agent)
    with tx(growth_agent) as c:
        ref = submit_work(c, growth_agent, TaskSpec(kind="recover", purpose="resource_probe", payload={"episode_root": original["task_id"], "condition_revision": "test"}, review_at=utcnow()+timedelta(hours=1)))
    runtime.finish(runtime.advance(ref)["claim"], WorkResult(outcome="completed", output={"state":"still_blocked", "reason":"access_denied", "resource":"site:original", "resumed_count":0}))
    recent = next(row for row in render_growth(growth_agent)["recent_work"] if str(row["id"]) == ref["task_id"])
    assert recent["state"] == "succeeded"
    assert recent["title"] == "检查受影响的能力"
    assert recent["probe"]["state"] == "still_blocked"
    assert recent["probe"]["reason"] == "access_denied"
    assert recent["probe"]["resumed_count"] == 0


def test_early_terminal_has_one_durable_notification(growth_agent):
    ref = new_work(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{spec,review_at}',%s) WHERE id=%s", (j((utcnow()-timedelta(seconds=1)).isoformat()), ref["task_id"]))
    assert runtime.advance(ref)["reason"] == "work_window_ended"
    assert runtime.advance(ref)["state"] == "terminal"
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'task_id'=%s AND payload->>'reason'='work_window_ended'", (ref["task_id"],))["n"] == 1


def test_unknown_original_action_is_not_requeued(growth_agent):
    from lead_app.growth.tools import prepare_channel
    from test_growth_channel_operation import channel_work
    root = blocked(growth_agent)
    ref, data, _ = channel_work(growth_agent)
    runtime.advance(ref)
    action = prepare_channel(growth_agent, ref["task_id"], data)
    runtime.check_research_resource(growth_agent, ref["task_id"], SCOPE)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s", (action["id"],))
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s", (action["id"],))
        c.execute("UPDATE lead.tasks SET state='failed' WHERE id=%s", (ref["task_id"],))
    resumed = consume(growth_agent, root)
    assert ref["task_id"] not in {item["task_id"] for item in resumed}
    with tx(growth_agent) as c:
        assert one(c, "SELECT input->>'task_generation' generation FROM lead.tasks WHERE id=%s", (ref["task_id"],))["generation"] == '1'
        assert one(c, "SELECT state FROM lead.actions WHERE id=%s", (action["id"],))["state"] == 'unknown'


def test_stale_probe_cannot_clear_new_failure(growth_agent):
    root = blocked(growth_agent)
    with tx(growth_agent) as c:
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (root["task_id"],))
        episode = row["result"]["runtime"]["recovery"]
        episode["probe_token"] = "original-probe"
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s", (j({**episode, "attempts":4}), root["task_id"]))
    assert runtime._consume_recovery(growth_agent, root["task_id"], episode, "old-proof", episode["condition_revision"], "original-probe") is None


def test_other_project_cannot_consume_recovery(growth_agent):
    from lead_app import domain
    root = blocked(growth_agent)
    with tx(growth_agent) as c:
        episode = one(c, "SELECT result#>'{runtime,recovery}' episode FROM lead.tasks WHERE id=%s", (root["task_id"],))["episode"]
    other = domain.create_agent(runtime_engine="growth")
    try:
        assert runtime._consume_recovery(other, root["task_id"], episode, "foreign-proof", episode["condition_revision"], "foreign-lease") is None
    finally:
        domain.set_control(other, "archive")


async def test_free_probe_is_bounded_without_model_cost(growth_agent, monkeypatch):
    from lead_app.growth import tools
    from lead_app.growth.tools import RecoverInput
    root = blocked(growth_agent)
    calls = []
    async def denied(*args):
        calls.append(1)
        return WorkResult(outcome="completed", output={"state":"still_blocked"})
    monkeypatch.setattr(tools, "probe_research_resource", denied, raising=False)
    with tx(growth_agent) as c:
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (root["task_id"],))
    data = RecoverInput(episode_root=root["task_id"], condition_revision=row["result"]["runtime"]["condition_revision"])
    for _ in range(8):
        assert (await runtime.probe_episode(growth_agent, row, data)).output["state"] == "still_blocked"
    for _ in range(2):
        result = await runtime.probe_episode(growth_agent, row, data)
        assert result.output["state"] == "recovery_budget_exhausted"
        assert result.output["reason"] == "recovery_probe_limit"
    assert len(calls) == 8
    runtime.recover_due(growth_agent, now=utcnow()+timedelta(minutes=40))
    with tx(growth_agent) as c:
        assert not one(c, "SELECT id FROM lead.tasks WHERE type='growth.recover'")
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='resource_probe_stopped'")["n"] == 1


async def test_expired_probe_window_never_calls_source(growth_agent, monkeypatch):
    from lead_app.growth import tools
    from lead_app.growth.tools import RecoverInput
    root = blocked(growth_agent)
    async def forbidden(*args):
        raise AssertionError("Expired original work must not spend another probe")
    monkeypatch.setattr(tools, "probe_research_resource", forbidden, raising=False)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery,probe_deadline}',%s) WHERE id=%s", (j((utcnow()-timedelta(seconds=1)).isoformat()),root["task_id"]))
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (root["task_id"],))
    result = await runtime.probe_episode(growth_agent, row, RecoverInput(episode_root=root["task_id"],condition_revision=row["result"]["runtime"]["condition_revision"]))
    assert result.output["reason"] == "recovery_deadline"


def test_claim_keeps_monotonic_project_control_version(growth_agent):
    ref = new_work(growth_agent)
    claim = runtime.advance(ref)["claim"]
    with tx(growth_agent) as c:
        saved = one(c, "SELECT result#>'{runtime,control_version}' version FROM lead.tasks WHERE id=%s", (ref["task_id"],))["version"]
        current = one(c, "SELECT control_version FROM lead.agents WHERE id=%s", (growth_agent,))["control_version"]
    assert claim["control_version"] == saved == current
