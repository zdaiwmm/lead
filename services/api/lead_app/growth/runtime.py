"""Fixed-reference work. Waiting never occupies an activity or another channel."""
import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from temporalio import activity

from ..db import all_rows, emit, j, one, require_agent, tx
from ..scope import scoped_activity
from .contracts import TaskRef, TaskSpec, Wait, WorkResult, utcnow
from .store import digest, enabled, objective_revision

TERMINAL = {"succeeded", "failed", "canceled"}
PERMANENT = {"unsupported_operation", "scope_changed", "invalid_evidence", "invalid_proposal",
             "dependency_failed", "account_changed", "target_not_permitted", "question_limit", "experiment_closed",
             "experiment_window_ended","product_changed","content_product_changed","stale_submission_claim",
             "unsupported_content_claim","unsupported_research_finding"}
MODEL_ERRORS={"ModelHTTPError","ModelRateLimitError","APITimeoutError","ConnectError","ReadTimeout",
    "APIConnectionError","ModelQuotaExceededError","AuthenticationError","ModelUnavailable","UnexpectedModelBehavior"}


def _ref(value):
    return TaskRef.model_validate(value)


def _runtime(row):
    return dict((row["result"] or {}).get("runtime") or {})


def _save(c, row, state, runtime, output=None, when=None):
    value = {"runtime": runtime}
    if output is not None:
        value["output"] = output
    c.execute("""UPDATE lead.tasks SET state=%s,result=coalesce(result,'{}') || %s,version=version+1,
        scheduled_at=coalesce(%s,scheduled_at),completed_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s""",
        (state, j(value), when, state in TERMINAL, row["id"]))


def _wait(row, reason, now, *, producer="growth.runtime", action="recheck", delay=60, owner="product"):
    return Wait(reason=reason, producer=producer, recovery_action=action, check_at=now + timedelta(seconds=delay),
                episode_root=row["id"], responsibility=owner).model_dump(mode="json")


def _resource_condition(c, agent, row, spec):
    """Conditions come from actual references/configuration, never a model-supplied retry key."""
    if spec.kind in ("content", "decision") or _runtime(row).get("failure_stage")=="model":
        from ..admin_settings import effective, task_connection
        settings, version = effective(c, "models", str(agent["id"]))
        from .agent import PROMPT_VERSION
        config,reference,_ = task_connection(c,str(agent["id"]),"planning" if spec.kind=="content" else "research")
        return "model:"+reference, digest([version, settings, PROMPT_VERSION, config["key"]])
    if spec.account_id or spec.payload.get("profile_id"):
        profile = row["input"].get("profile_id") or spec.payload.get("profile_id")
        route = one(c, "SELECT generation FROM lead.profile_execution_routes WHERE profile_id=%s", (profile,))
        account = one(c,"""SELECT s.binding_version,s.state,i.revision,i.desired,i.scope FROM lead.social_accounts s
            LEFT JOIN lead.account_operating_intents i ON i.account_id=s.id WHERE s.id=%s""",(spec.account_id,)) if spec.account_id else None
        capability = None
        if spec.kind=="channel":
            capability = one(c,"""SELECT t.state,t.verified_permissions->>'adapter_version' adapter_version,
                g.id grant_id,g.version,g.allowed_actions,g.limits,pc.permission_state,pc.technical_state,
                pc.adapter_version installed_version FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id
                LEFT JOIN lead.platform_capabilities pc ON pc.platform=s.platform AND pc.target_type=t.target_type AND pc.action_type='publish'
                LEFT JOIN LATERAL (SELECT * FROM lead.authorization_grants WHERE target_id=t.id AND revoked_at IS NULL ORDER BY version DESC LIMIT 1) g ON true
                WHERE t.id=%s""",(spec.target_id,))
        return "profile:"+str(profile), digest([account,(route or {}).get("generation"),capability])
    urls = spec.payload.get("urls") or []
    if spec.kind=="research" and spec.payload.get("queries"):
        return "research:public_search",digest("https://html.duckduckgo.com/html/")
    if not urls and spec.kind == "research":
        business = one(c, "SELECT product_url FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
        urls = [business["product_url"]] if business and business["product_url"] else []
    return spec.kind+":"+digest(sorted({urlsplit(url).hostname for url in urls})), digest(urls)


def _proposal_key(row):
    spec=row["input"].get("spec",{})
    return digest([row["type"],row["input"].get("business_profile_id"),row["input"].get("goal_revision"),
        [{key:value for key,value in trigger.items() if not key.startswith("_")}
            for trigger in spec.get("payload",{}).get("triggers",[])],spec.get("evidence_refs",[])])


def _episode(c, resource, condition,row,code=None):
    return one(c, """SELECT id,result#>'{runtime,recovery}' recovery FROM lead.tasks
        WHERE result#>>'{runtime,recovery,resource}'=%s
          AND result#>>'{runtime,recovery,condition_revision}'=%s
          AND (%s::text IS NULL OR result#>>'{runtime,recovery,error_code}'=%s)
          AND (result#>>'{runtime,recovery,error_code}'<>'invalid_proposal'
            OR result#>>'{runtime,recovery,proposal_key}'=%s)
        ORDER BY (result#>>'{runtime,recovery,status}'='blocked') DESC,created_at,id LIMIT 1""",
        (resource, condition,code,code,_proposal_key(row)))


def _record_failure(c, row, runtime, code):
    resource, condition = runtime.get("resource_key", row["type"]), runtime.get("condition_revision", "initial")
    prior = _episode(c, resource, condition,row,code)
    if not prior and row["type"]=="growth.research" and code=="source_unavailable":
        prior = _episode(c,resource,condition,row,"gaierror")
    root = prior["id"] if prior else row["id"]
    episode = dict(prior["recovery"]) if prior else {
        "fingerprint": digest([resource, condition, code]), "resource": resource,
        "condition_revision": condition, "root_task_id": str(root), "attempts": 0,
        "repair_attempts": 0, "probes": 0,"proposal_key":_proposal_key(row)}
    episode["attempts"] += 1
    episode.update(error_code=code, status="blocked" if episode["attempts"] >= 3 or code in PERMANENT else "recovering",
                   next_check_at=(utcnow()+timedelta(minutes=min(60, 5*2**episode["probes"]))).isoformat())
    runtime["episode_root"] = str(root)
    if root == row["id"]:
        runtime["recovery"] = episode
    else:
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s", (j(episode), root))
    return episode


def advance(value, attempt_token=None, *, now=None):
    ref = _ref(value)
    now = now or utcnow()
    with tx(str(ref.agent_id)) as c:
        agent = require_agent(c, str(ref.agent_id))
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE", (ref.task_id,))
        if not row or row["input"].get("task_generation") != ref.task_generation:
            return {"state": "terminal", "reason": "stale_task"}
        if row["state"] in TERMINAL:
            return {"state": "terminal", "reason": row["state"]}
        runtime = _runtime(row)
        spec = TaskSpec.model_validate(row["input"]["spec"])
        if not enabled(agent):
            _save(c, row, "canceled", {**runtime, "reason": "legacy_owns_scope"})
            return {"state": "terminal", "reason": "legacy_owns_scope"}
        if agent["onboarding_state"].get("home_release_maintenance"):
            wait = _wait(row,"release_maintenance",now,producer="home_release_maintenance",owner="resource")
            _save(c,row,"planned",{**runtime,"wait":wait},when=wait["check_at"])
            return {"state":"wait","wait":wait}
        original = one(c, "SELECT id,state FROM lead.actions WHERE task_id=%s ORDER BY created_at DESC LIMIT 1", (row["id"],))
        reconcile = bool(original and original["state"] in ("dispatching", "unknown", "confirmed"))
        if spec.kind=="observe" and spec.payload.get("legacy_native_task_id"):
            native = one(c,"SELECT input,result FROM lead.tasks WHERE id=%s",(spec.payload["legacy_native_task_id"],))
            reconcile = bool(native and native["input"].get("migration",{}).get("owner")=="receipt_only")
        if agent["desired_state"] in ("paused", "archived") and not reconcile and spec.kind not in ("account","observe","recover"):
            _save(c, row, "canceled", {**runtime, "reason": "project_paused"})
            return {"state": "terminal", "reason": "project_paused"}
        if row["state"] == "running" and runtime.get("lease_until") and now < datetime.fromisoformat(runtime["lease_until"]):
            return {"state": "wait", "wait": _wait(row, "work_leased", now, delay=15)}
        wait = runtime.get("wait")
        if wait and now < datetime.fromisoformat(wait["check_at"]):
            return {"state": "wait", "wait": wait}
        if spec.review_at <= now and not reconcile and spec.kind not in ("observe", "recover"):
            _save(c, row, "failed", {**runtime, "reason": "work_window_ended"})
            return {"state": "terminal", "reason": "work_window_ended"}
        if spec.experiment_id:
            experiment = one(c, "SELECT state,specification FROM lead.growth_experiments WHERE id=%s", (spec.experiment_id,))
            if not experiment or experiment["state"] not in ("active", "observing"):
                if not reconcile:
                    _save(c, row, "canceled", {**runtime, "reason": "experiment_closed"})
                    return {"state": "terminal", "reason": "experiment_closed"}
        product_research=spec.kind=="research" and spec.purpose=="product" and not spec.experiment_id
        if spec.kind in ("research","content","channel") and not product_research and not reconcile and row["input"].get("goal_revision")!=objective_revision(agent):
            _save(c,row,"canceled",{**runtime,"reason":"objective_changed"})
            return {"state":"terminal","reason":"objective_changed"}
        if spec.kind in ("research","content","channel") and not reconcile and "business_profile_id" in row["input"]:
            business = one(c,"SELECT id FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
            if row["input"]["business_profile_id"]!=(str(business["id"]) if business else None):
                _save(c,row,"canceled",{**runtime,"reason":"product_changed"})
                emit(c,str(ref.agent_id),"growth.wake",{"reason":"product_changed"})
                return {"state":"terminal","reason":"product_changed"}
        resource, condition = _resource_condition(c, agent, row, spec)
        runtime.update(resource_key=resource, condition_revision=condition)
        episode = _episode(c, resource, condition,row)
        if episode and episode["recovery"]["status"] == "blocked" and not reconcile:
            _save(c, row, "failed", {**runtime, "episode_root": str(episode["id"]), "reason": "recovery_exhausted"})
            return {"state": "terminal", "reason": "recovery_exhausted"}
        for dependency in spec.required_refs:
            before = one(c, "SELECT state,result FROM lead.tasks WHERE id=%s", (dependency.task_id,))
            if not before or before["state"] in ("failed", "canceled"):
                _save(c, row, "failed", {**runtime, "reason": "dependency_failed"})
                emit(c, str(ref.agent_id), "growth.wake", {"reason": "dependency_failed", "task_id": str(row["id"])})
                return {"state": "terminal", "reason": "dependency_failed"}
            if before["state"] != "succeeded":
                wait = _wait(row, "dependency_pending", now)
                _save(c, row, "planned", {**runtime, "wait": wait}, when=wait["check_at"])
                return {"state": "wait", "wait": wait}
            produced = (before["result"] or {}).get("output") or {}
            if dependency.artifact!="output" and not any(
                item.get("kind")==dependency.artifact for item in produced.get("artifacts",[])+produced.get("evidence_refs",[])):
                _save(c,row,"failed",{**runtime,"reason":"dependency_artifact_missing"})
                emit(c,str(ref.agent_id),"growth.wake",{"reason":"dependency_artifact_missing","task_id":str(row["id"])})
                return {"state":"terminal","reason":"dependency_artifact_missing"}
        if (spec.account_id or spec.payload.get("profile_id")) and not reconcile:
            account = one(c, """SELECT s.*,i.desired FROM lead.social_accounts s
                LEFT JOIN lead.account_operating_intents i ON i.account_id=s.id WHERE s.id=%s""", (spec.account_id,))
            if spec.account_id and (not account or account["binding_version"] != row["input"].get("binding_version")):
                _save(c, row, "canceled", {**runtime, "reason": "account_changed"})
                return {"state": "terminal", "reason": "account_changed"}
            if account and account["desired"] != "running":
                wait = _wait(row, "account_paused", now, producer="account_intents", action="configuration", owner="owner")
                _save(c, row, "planned", {**runtime, "wait": wait}, when=wait["check_at"])
                return {"state": "wait", "wait": wait}
            resource = "profile:" + str(account["profile_id"] if account else spec.payload["profile_id"])
            # Short admission serialization; never hold this lock across browser IO.
            if not one(c, "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) ok", (resource,))["ok"]:
                return {"state": "wait", "wait": _wait(row, "resource_wait", now, delay=1, owner="resource")}
            busy = one(c, """SELECT id FROM lead.tasks WHERE id<>%s AND state='running'
                AND result#>>'{runtime,resource}'=%s
                AND (result#>>'{runtime,lease_until}')::timestamptz>now() LIMIT 1""", (row["id"], resource))
            if busy:
                wait = _wait(row, "resource_wait", now, delay=1, owner="resource")
                _save(c, row, "queued", {**runtime, "wait": wait}, when=wait["check_at"])
                return {"state": "wait", "wait": wait}
            runtime["resource"] = resource
        attempts = runtime.get("attempts", 0)
        if runtime.get("failure_attempts", 0) >= 3 and not runtime.get("recovery_permit") and not reconcile:
            _save(c, row, "failed", {**runtime, "reason": "recovery_exhausted"})
            return {"state": "terminal", "reason": "recovery_exhausted"}
        token = str(uuid4())
        if runtime.get("recovery_permit"):
            runtime["recovery_permit"] -= 1
        runtime.update(attempt_token=token, attempts=attempts + 1, wait=None,
                       lease_until=(now + timedelta(minutes=9)).isoformat(),
                       last_progress_at=now.isoformat(), operation_ref=str(original["id"]) if original else None)
        _save(c, row, "running", runtime)
        c.execute("UPDATE lead.tasks SET started_at=coalesce(started_at,now()) WHERE id=%s", (row["id"],))
        claim = {"ref": ref.model_dump(mode="json"), "attempt_token": token,
                 "lease_until": runtime["lease_until"], "operation_ref": runtime["operation_ref"], "kind": spec.kind}
        return {"state": "ready", "claim": claim}


def finish(claim, value):
    result = WorkResult.model_validate(value)
    ref = _ref(claim["ref"])
    with tx(str(ref.agent_id)) as c:
        initial = one(c, "SELECT result FROM lead.tasks WHERE id=%s", (ref.task_id,))
        if initial and result.outcome == "failed":
            key = initial["result"].get("runtime", {}).get("resource_key", str(ref.task_id))
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("growth-recovery:"+key,))
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE", (ref.task_id,))
        if not row:
            return {"accepted_control": False, "evidence_recorded": False}
        runtime = _runtime(row)
        # A late external observation remains associated with the old action, never a new send.
        if (runtime.get("attempt_token") != claim["attempt_token"]
                or row["input"].get("task_generation") != ref.task_generation or row["state"] != "running"
                or not runtime.get("lease_until") or datetime.fromisoformat(runtime["lease_until"])<=utcnow()):
            if result.evidence_refs:
                from .store import validate_refs
                validate_refs(c, result.evidence_refs)
                from ..db import audit
                audit(c, str(ref.agent_id), "growth.late_evidence",
                      {"task_id": str(row["id"]), "evidence_refs": [x.model_dump(mode="json") for x in result.evidence_refs]})
            return {"accepted_control": False, "evidence_recorded": bool(result.evidence_refs)}
        runtime.update(lease_until=None, attempt_token=None)
        action = one(c, "SELECT id,state FROM lead.actions WHERE task_id=%s ORDER BY created_at DESC LIMIT 1", (row["id"],))
        valid_receipt = bool(action and action["state"]=="confirmed" and one(c,"SELECT lead.growth_receipt_valid(%s) ok",(action["id"],))["ok"])
        if row["type"] == "growth.channel" and result.outcome == "completed" and not valid_receipt:
            result = WorkResult(outcome="unknown" if action and action["state"] in ("dispatching","unknown") else "failed",
                                failure_code="invalid_evidence")
        if row["type"]=="growth.observe" and row["input"]["spec"]["payload"].get("legacy_native_task_id") and result.outcome=="completed":
            native = one(c,"SELECT result FROM lead.tasks WHERE id=%s",(row["input"]["spec"]["payload"]["legacy_native_task_id"],))
            saved = (native or {}).get("result") or {}
            if not ((saved.get("reply_state")=="confirmed" and saved.get("reply_external_id") and saved.get("reply_confirmed_at"))
                or (saved.get("join_state")=="confirmed" and saved.get("join_confirmed_at"))
                or (saved.get("reply_state")=="failed" and saved.get("reply_failed_at"))):
                result = WorkResult(outcome="failed",failure_code="invalid_evidence")
        if result.outcome == "failed" and action and action["state"] in ("dispatching", "unknown"):
            result = WorkResult(outcome="unknown", failure_code="unknown_receipt")
        if result.outcome == "failed":
            if row["type"]=="growth.research":
                runtime["failure_stage"]="model" if result.failure_code in MODEL_ERRORS else None
                resource,condition=_resource_condition(c,require_agent(c,str(ref.agent_id)),
                    {**row,"result":{**row["result"],"runtime":runtime}},TaskSpec.model_validate(row["input"]["spec"]))
                runtime.update(resource_key=resource,condition_revision=condition)
            runtime["failure_attempts"] = runtime.get("failure_attempts", 0) + 1
            if result.failure_code not in PERMANENT or result.failure_code == "invalid_proposal":
                episode = _record_failure(c, row, runtime, result.failure_code or "tool_failed")
                if episode["status"] == "blocked":
                    runtime["failure_attempts"] = max(3, runtime["failure_attempts"])
        if result.checkpoint is not None:
            runtime["checkpoint"] = result.checkpoint
        output = result.model_dump(mode="json")
        if result.outcome == "completed":
            _save(c, row, "succeeded", {**runtime, "wait": None}, output)
            if row["type"]=="growth.decision":
                c.execute("""UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery,status}','\"superseded\"')
                    WHERE type='growth.decision' AND state='failed' AND result#>>'{runtime,recovery,error_code}'='invalid_proposal'
                    AND result#>>'{runtime,recovery,status}'='blocked'""")
            if (row["type"] in ("growth.account","growth.channel") or result.output.get("knowledge_progress") is True
                or result.output.get("learning_progress") is True or result.output.get("business_progress") is True
                or result.output.get("context_available") is True):
                emit(c, str(ref.agent_id), "growth.wake", {"reason": "evidence_added", "task_id": str(row["id"])})
        elif result.outcome == "unknown":
            wait = _wait(row, "unknown_receipt", utcnow(), action="reconcile", delay=300)
            _save(c, row, "reconciling", {**runtime, "wait": wait}, output, wait["check_at"])
        elif result.outcome == "waiting":
            wait = result.wait.model_dump(mode="json")
            state = "waiting_user" if result.wait.recovery_action == "owner" else "planned"
            _save(c, row, state, {**runtime, "wait": wait}, output, wait["check_at"])
        elif result.failure_code in PERMANENT or runtime.get("failure_attempts", 0) >= 3:
            _save(c, row, "failed", {**runtime, "reason": result.failure_code}, output)
            emit(c, str(ref.agent_id), "growth.wake", {"reason": "work_failed", "task_id": str(row["id"])})
        else:
            wait = _wait(row, result.failure_code or "tool_failed", utcnow(),
                         delay=min(30, 2 ** runtime["attempts"]))
            _save(c, row, "planned", {**runtime, "wait": wait}, output, wait["check_at"])
        return {"accepted_control": True, "evidence_recorded": True}


async def execute(claim):
    from ..budget import BudgetExceeded, available
    from .tools import execute_tool
    ref = _ref(claim["ref"])
    with tx(str(ref.agent_id)) as c:
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (ref.task_id,))
        if not row or _runtime(row).get("attempt_token") != claim["attempt_token"]:
            return WorkResult(outcome="failed", failure_code="scope_changed").model_dump(mode="json")
    try:
        from ..budget import growth_cost_context
        cost_token = growth_cost_context.set({"task_id": str(ref.task_id), "experiment_id": str(row["experiment_id"]) if row["experiment_id"] else None})
        try:
            result = await asyncio.wait_for(execute_tool(str(ref.agent_id), row, claim), timeout=480)
            return WorkResult.model_validate(result).model_dump(mode="json")
        finally:
            growth_cost_context.reset(cost_token)
    except BudgetExceeded as error:
        code = str(error)
        if code in ("question_limit","experiment_closed","experiment_window_ended"):
            return WorkResult(outcome="failed",failure_code=code).model_dump(mode="json")
        with tx(str(ref.agent_id)) as c:
            end = available(c,str(ref.agent_id))["period_ends_at"]
        wait = Wait(reason=code,producer="budget",recovery_action="configuration" if code=="model_call_billing_unknown" else "recheck",
                    check_at=end,episode_root=ref.task_id,scope_ref=str(row["experiment_id"] or ref.task_id),
                    escalation="Reconcile the original billing reservation or wait for the configured budget period; independent deterministic work continues.")
        return WorkResult(outcome="waiting",wait=wait).model_dump(mode="json")
    except Exception as error:
        with tx(str(ref.agent_id)) as c:
            action = one(c, "SELECT id FROM lead.actions WHERE task_id=%s AND state IN ('dispatching','unknown')", (ref.task_id,))
        if action:
            return WorkResult(outcome="unknown", failure_code="unknown_receipt").model_dump(mode="json")
        code = str(error) if isinstance(error, ValueError) and len(str(error)) < 120 else type(error).__name__
        return WorkResult(outcome="failed", failure_code=code).model_dump(mode="json")


def recover_due(aid, now=None, cursor=None):
    now = now or utcnow()
    checked = []
    with tx(aid) as c:
        from .store import experiment_progress, question_costs, submit_work
        for experiment in all_rows(c,"""SELECT * FROM lead.growth_experiments WHERE state IN ('active','observing')
            ORDER BY recovery_checked_at,id LIMIT 20 FOR UPDATE SKIP LOCKED"""):
            c.execute("UPDATE lead.growth_experiments SET recovery_checked_at=%s WHERE id=%s",(now,experiment["id"]))
            costs = question_costs(c,experiment["question_key"])
            expired = datetime.fromisoformat(experiment["specification"]["review_at"])<=now
            if not expired and costs["committed_units"] < costs["limit_units"]:
                continue
            progress = experiment_progress(c,experiment["id"])
            c.execute("""UPDATE lead.growth_experiments SET state='closed',closed_at=now(),updated_at=now(),revision=revision+1,
                conclusion=%s WHERE id=%s""",(j({"state":"inconclusive","reason":"window_ended" if expired else "working_budget_ended",
                    "verified_progress":progress,"costs":costs}),experiment["id"]))
            c.execute("""UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE experiment_id=%s
                AND state IN ('planned','queued','waiting_user') AND NOT EXISTS(SELECT FROM lead.actions a
                    WHERE a.task_id=lead.tasks.id AND a.state IN ('dispatching','unknown','confirmed'))""",(experiment["id"],))
            c.execute("UPDATE lead.actions SET state='canceled' WHERE state='prepared' AND task_id IN(SELECT id FROM lead.tasks WHERE experiment_id=%s)",(experiment["id"],))
            emit(c,aid,"growth.wake",{"reason":"experiment_review_due","experiment_id":str(experiment["id"]),"revision":experiment["revision"]+1})
        for root in all_rows(c, """SELECT * FROM lead.tasks WHERE type LIKE 'growth.%%'
            AND result#>>'{runtime,recovery,status}'='blocked'
            AND (result#>>'{runtime,recovery,next_check_at}')::timestamptz<=%s
            ORDER BY scheduled_at,id LIMIT 20 FOR UPDATE SKIP LOCKED""", (now,)):
            episode = root["result"]["runtime"]["recovery"]
            if episode.get("paid_probes_stopped") or episode.get("error_code")=="invalid_proposal":
                agent = require_agent(c,aid)
                _, condition = _resource_condition(c,agent,root,TaskSpec.model_validate(root["input"]["spec"]))
                if condition == episode["condition_revision"]:
                    episode["next_check_at"]=(now+timedelta(hours=1)).isoformat()
                    c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s",(j(episode),root["id"]))
                    continue
            submit_work(c, aid, TaskSpec(kind="recover",purpose="resource_probe",priority="background",
                payload={"episode_root":str(root["id"]),"condition_revision":episode["condition_revision"]},
                review_at=now+timedelta(hours=2),dedupe_key=episode["next_check_at"]))
            episode["next_check_at"]=(now+timedelta(hours=1)).isoformat()
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s",(j(episode),root["id"]))
        rows = all_rows(c, """SELECT * FROM lead.tasks WHERE type LIKE 'growth.%%'
            AND (state IN ('planned','queued','reconciling','waiting_user') AND scheduled_at<=%s
                OR state='running' AND (result#>>'{runtime,lease_until}')::timestamptz<=%s)
            ORDER BY scheduled_at,id LIMIT 20 FOR UPDATE SKIP LOCKED""", (now, now))
        for row in rows:
            runtime = _runtime(row)
            if row["state"] == "running":
                action = one(c, "SELECT id FROM lead.actions WHERE task_id=%s AND state IN ('dispatching','unknown')", (row["id"],))
                if not action:
                    runtime["failure_attempts"] = runtime.get("failure_attempts",0)+1
                    episode = _record_failure(c,row,runtime,"work_lease_expired")
                state = "reconciling" if action else "failed" if episode["status"]=="blocked" else "planned"
                runtime.update(lease_until=None, attempt_token=None, wait=None)
                _save(c, row, state, runtime)
            checked.append(str(row["id"]))
            emit(c, aid, "growth.work", {"agent_id": str(aid), "task_id": str(row["id"]),
                                      "task_generation": row["input"]["task_generation"]})
            # Rotate even waiting rows so a full batch cannot starve the next page.
            c.execute("UPDATE lead.tasks SET scheduled_at=%s WHERE id=%s", (now + timedelta(seconds=60), row["id"]))
    return {"checked_refs": checked, "next_cursor": checked[-1] if checked else cursor,
            "next_run": (now + timedelta(seconds=60)).isoformat()}


def resume_task(aid, task_id, *, proof_ref=None):
    """Only changed server conditions or a matching successful probe can restart failed work."""
    with tx(aid) as c:
        agent = require_agent(c,aid,True)
        row = one(c,"SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE",(task_id,))
        if not row or row["state"] != "failed":
            return None
        if one(c,"SELECT id FROM lead.actions WHERE task_id=%s AND state IN ('dispatching','unknown','confirmed')",(task_id,)):
            raise ValueError("reconcile_original_action")
        runtime = _runtime(row)
        spec = TaskSpec.model_validate(row["input"]["spec"])
        _, condition = _resource_condition(c,agent,row,spec)
        episode = runtime.get("recovery") or {}
        changed = condition != runtime.get("condition_revision")
        matching_probe = bool(proof_ref and episode.get("proof_ref")==proof_ref and episode.get("status")=="recovered" and not episode.get("resumed"))
        if not changed and not matching_probe:
            raise ValueError("recovery_condition_unchanged")
        if changed:
            runtime["recovery_history"] = [*runtime.get("recovery_history",[]),episode]
            runtime.update(recovery=None, failure_attempts=0, condition_revision=condition)
        else:
            episode["resumed"] = True
            runtime.update(recovery=episode,recovery_permit=1)
        runtime.update(wait=None,lease_until=None,attempt_token=None,reason="condition_recovered")
        new_input = {**row["input"],"task_generation":row["input"]["task_generation"]+1}
        c.execute("UPDATE lead.tasks SET input=%s WHERE id=%s",(j(new_input),task_id))
        _save(c,row,"planned",runtime,when=utcnow())
        ref = {"agent_id":str(aid),"task_id":str(task_id),"task_generation":new_input["task_generation"]}
        emit(c,aid,"growth.work",ref)
        return ref


async def probe_episode(aid, work, data):
    with tx(aid) as c:
        agent = require_agent(c,aid)
        root = one(c,"SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE",(data.episode_root,))
        if not root:
            raise ValueError("recovery_scope")
        episode = dict(_runtime(root).get("recovery") or {})
        if episode.get("status") != "blocked" or episode.get("condition_revision") != data.condition_revision:
            return WorkResult(outcome="completed",output={"state":"obsolete_probe"})
        if episode.get("probe_lease_until") and datetime.fromisoformat(episode["probe_lease_until"])>utcnow():
            return WorkResult(outcome="completed",output={"state":"another_probe_active"})
        spec = TaskSpec.model_validate(root["input"]["spec"])
        _, actual_condition = _resource_condition(c,agent,root,spec)
        changed = actual_condition != episode["condition_revision"]
        if episode.get("paid_probes_stopped") and not changed:
            return WorkResult(outcome="completed",output={"state":"recovery_budget_exhausted","responsibility":"product"})
        episode.update(probes=episode.get("probes",0)+1,probe_lease_until=(utcnow()+timedelta(minutes=9)).isoformat())
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s",(j(episode),root["id"]))
    proof = None
    try:
        if changed:
            resumed = await asyncio.to_thread(resume_task,aid,root["id"])
            return WorkResult(outcome="completed",output={"state":"configuration_changed","resumed":resumed})
        if episode.get("error_code") in ("source_unavailable","gaierror") and spec.kind == "research":
            from ..budget import growth_cost_context
            from .tools import research
            token = growth_cost_context.set({"task_id":str(root["id"]),
                "experiment_id":str(root["experiment_id"]) if root["experiment_id"] else None,
                "recovery_root":str(root["id"]),"recovery_limit":70000})
            try:
                observation = await research(aid,root,spec)
            finally:
                growth_cost_context.reset(token)
            if observation.outcome == "completed" and observation.evidence_refs:
                proof = "document:"+str(observation.evidence_refs[0].id)
        elif episode.get("error_code") in ("ModelHTTPError","ModelRateLimitError","APITimeoutError","ConnectError","ReadTimeout","APIConnectionError") and spec.kind in ("decision","content","research"):
            from ..budget import growth_cost_context
            from ..model import run_model
            from .contracts import Contract
            class HealthProbe(Contract):
                ready: bool
            token = growth_cost_context.set({"task_id":str(work["id"]),"recovery_root":str(root["id"]),
                                              "recovery_limit":70000})
            try:
                check = await run_model(HealthProbe,{"request":"Return ready=true to confirm that this configured model can complete a structured request."},
                                        system="Return the requested health response; no tools or business action.",agent_id=aid,purpose="research",short_request=True)
                if check.ready:
                    proof = "provider-structured-response:"+str(work["id"])
            finally:
                growth_cost_context.reset(token)
        if proof:
            with tx(aid) as c:
                episode.update(status="recovered",proof_ref=proof,probe_lease_until=None)
                c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s",(j(episode),root["id"]))
                emit(c,aid,"growth.wake",{"reason":"resource_recovered","fingerprint":episode["fingerprint"],"proof_ref":proof})
            resumed = await asyncio.to_thread(resume_task,aid,root["id"],proof_ref=proof)
            return WorkResult(outcome="completed",output={"state":"recovered","resumed":resumed})
        return WorkResult(outcome="completed",output={"state":"still_blocked","responsibility":"product",
            "condition_revision":episode["condition_revision"],"next_check_at":episode["next_check_at"]})
    except Exception as error:
        from ..budget import BudgetExceeded
        if not isinstance(error,BudgetExceeded) or str(error)!="recovery_budget_exhausted":
            raise
        episode.update(paid_probes_stopped=True,reason="recovery_budget_exhausted",
                       responsibility="product",recovery_action="configuration")
        with tx(aid) as c:
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s",(j(episode),root["id"]))
        return WorkResult(outcome="completed",output={"state":"recovery_budget_exhausted","responsibility":"product"})
    finally:
        with tx(aid) as c:
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery,probe_lease_until}','null') WHERE id=%s",(root["id"],))


def ensure_decision(aid, triggers=None):
    from .store import submit_work
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        if not enabled(agent) or agent["desired_state"]=="archived":
            return None
        # The owner lock is short and never spans model/tool execution.
        state = dict(agent["onboarding_state"].get("growth_runtime") or {})
        pending = dict(state.get("pending_triggers") or {})
        consumed = state.get("consumed_triggers") or {}
        for trigger in triggers or []:
            if trigger.get("reason") in ("heartbeat", "page_view"):
                continue
            key = trigger_key(trigger)
            if consumed.get(key)!=trigger_digest(trigger):
                pending[key] = {**trigger,"_queued_at":pending.get(key,{}).get("_queued_at") or utcnow().isoformat()}
        state["pending_triggers"] = pending
        c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{growth_runtime}',%s) WHERE id=%s", (j(state), aid))
        if agent["desired_state"]=="paused":
            return None
        old = one(c, """SELECT id,input FROM lead.tasks WHERE type='growth.decision'
            AND state IN ('planned','queued','running','waiting_user') ORDER BY created_at LIMIT 1""")
        if old:
            return {"agent_id":str(aid),"task_id":str(old["id"]),"task_generation":old["input"]["task_generation"]}
        if not pending:
            return None
        return submit_work(c, aid, TaskSpec(kind="decision", purpose="growth",
            payload={"triggers": sorted(pending.values(),key=lambda item:item.get("_queued_at",""))[:8]}, dedupe_key=digest(sorted(pending)),
            review_at=utcnow() + timedelta(hours=1)))


def consume_triggers(aid, triggers):
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        state = dict(agent["onboarding_state"].get("growth_runtime") or {})
        pending = dict(state.get("pending_triggers") or {})
        consumed = dict(state.get("consumed_triggers") or {})
        for item in triggers:
            key = trigger_key(item)
            consumed[key] = trigger_digest(item)
            if key in pending and trigger_digest(pending[key])==trigger_digest(item):
                pending.pop(key,None)
        state["pending_triggers"] = pending
        state["consumed_triggers"] = consumed
        c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{growth_runtime}',%s) WHERE id=%s", (j(state), aid))
    # Any fact arriving during the decision remains for a subsequent bounded decision.
    return ensure_decision(aid)


def trigger_key(item):
    # Coalesce a stream by changed business condition, retaining its latest fact.
    # A newer fact with the same key survives consumption of the old snapshot.
    task = item.get("task_id") if item.get("reason") in ("evidence_added","work_failed","dependency_failed","dependency_artifact_missing") else None
    return digest([item.get("reason"),item.get("account_id"),item.get("experiment_id"),item.get("event_name"),task])


def trigger_digest(item):
    return digest({key:value for key,value in item.items() if not key.startswith("_")})


@activity.defn
@scoped_activity
async def growth_advance(args: dict):
    return await asyncio.to_thread(advance, args)


@activity.defn
@scoped_activity
async def growth_execute(args: dict):
    return await execute(args["claim"])


@activity.defn
@scoped_activity
async def growth_finish(args: dict):
    result = await asyncio.to_thread(finish, args["claim"], args["result"])
    if result["accepted_control"] and args["claim"].get("kind") == "decision":
        with tx(args["agent_id"]) as c:
            row = one(c, "SELECT input,state FROM lead.tasks WHERE id=%s", (args["claim"]["ref"]["task_id"],))
        if row and row["state"] in TERMINAL:
            await asyncio.to_thread(consume_triggers, args["agent_id"], row["input"]["spec"]["payload"].get("triggers", []))
    return result


@activity.defn
@scoped_activity
async def growth_coordinate(args: dict):
    return await asyncio.to_thread(ensure_decision, args["agent_id"], args.get("triggers"))


def recover_workspace():
    with tx() as c:
        ids = [str(row["id"]) for row in all_rows(c, """SELECT id FROM lead.agents
            WHERE onboarding_state#>>'{growth_runtime,decision,engine}'='growth' AND desired_state<>'archived'""")]
    for aid in ids:
        recover_due(aid)
        ensure_decision(aid)
