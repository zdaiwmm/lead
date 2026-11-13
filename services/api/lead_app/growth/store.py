"""Canonical scoped writes: one task identity, independent atomic command groups."""
import hashlib
import json
from collections import defaultdict
from uuid import UUID

from psycopg import DataError, IntegrityError, sql
from pydantic import ValidationError

from ..db import audit, emit, j, one, require_agent, tx
from ..scope import current_tenant
from .contracts import DecisionProposal, ExperimentSpec, GoalRef, TaskRef, TaskSpec


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def command_group(command):
    scoped = command.experiment_id or (command.payload.get("experiment_id") if command.kind=="task.create" else None)
    return str(scoped) if scoped else command.group_key


def objective_revision(agent):
    return max(1, int(agent["onboarding_state"].get("operating_contract", {}).get("revision", 1)))


def enabled(agent):
    return agent["onboarding_state"].get("growth_runtime", {}).get("decision", {}).get("engine") == "growth"


def legacy_owns(agent):
    return agent["onboarding_state"].get("growth_runtime",{}).get("decision",{}).get("engine") in (None,"legacy")


def project_control(c,aid,action,agent):
    from .contracts import utcnow
    state = {"pause":"paused","start":"running","resume":"running","archive":"archived"}[action]
    if agent["desired_state"]==state:
        return False
    runtime = dict(agent["onboarding_state"].get("growth_runtime") or {})
    runtime["global_control"]={"source":"owner_command","desired":state,"changed_at":utcnow().isoformat()}
    c.execute("""UPDATE lead.agents SET desired_state=%s,health_state=%s,control_version=control_version+1,
        version=version+1,onboarding_state=jsonb_set(onboarding_state,'{growth_runtime}',%s),updated_at=now() WHERE id=%s""",
        (state,"queued" if state=="running" else "stopped",j(runtime),aid))
    if state in ("paused","archived"):
        c.execute("UPDATE lead.actions SET state='canceled' WHERE state='prepared'")
        c.execute("""UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE type LIKE 'growth.%%'
            AND type NOT IN ('growth.account','growth.observe','growth.recover')
            AND state IN ('planned','queued','running','waiting_user') AND NOT EXISTS(SELECT FROM lead.actions a
                WHERE a.task_id=lead.tasks.id AND a.state IN ('dispatching','unknown','confirmed'))""")
    if state=="archived":
        c.execute("UPDATE lead.social_accounts SET state='disconnected',automation_enabled=false WHERE state<>'disconnected'")
        c.execute("UPDATE lead.authorization_grants SET revoked_at=now() WHERE revoked_at IS NULL")
        c.execute("UPDATE lead.account_targets SET state='disabled' WHERE state='active'")
        c.execute("UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state IN ('requested','active','ending')")
        c.execute("SELECT lead.release_proxy(%s)",(aid,))
    audit(c,aid,"agent."+action,{"source":"owner_command","runtime":"growth"})
    from ..db import record
    record(c,aid,{"pause":"已暂停","resume":"已恢复自主运营","start":"开始自主运营","archive":"已归档"}[action])
    if state=="running":
        from .migration import bootstrap
        bootstrap(c,aid)
        emit(c,aid,"growth.wake",{"reason":"project_control","control_version":agent["control_version"]+1})
    return True


def validate_refs(c, refs):
    if not one(c,"SELECT lead.ctx_agent() id")["id"]:
        raise ValueError("explicit_agent_scope_required")
    tables = {"business": "business_profiles", "task": "tasks", "document": "knowledge_documents",
              "action": "actions", "event": "pixel_receipts"}
    for ref in refs:
        row = one(c, sql.SQL("SELECT id FROM lead.{} WHERE id=%s").format(sql.Identifier(tables[ref.kind])), (ref.id,))
        if not row:
            raise ValueError("invalid_evidence")


def validate_task(c, spec):
    from .tools import INPUTS
    if spec.kind in INPUTS:
        INPUTS[spec.kind].model_validate(spec.payload)
    validate_refs(c, spec.evidence_refs)
    if spec.account_id:
        account = one(c, "SELECT id FROM lead.social_accounts WHERE id=%s", (spec.account_id,))
        if not account:
            raise ValueError("account_scope")
    if spec.target_id and not one(c, "SELECT id FROM lead.account_targets WHERE id=%s AND account_id=%s",
                                  (spec.target_id, spec.account_id)):
        raise ValueError("target_scope")
    if spec.experiment_id and not one(c, "SELECT id FROM lead.growth_experiments WHERE id=%s AND state IN ('active','observing')",
                                     (spec.experiment_id,)):
        raise ValueError("experiment_closed")
    for dependency in spec.required_refs:
        row = one(c, "SELECT id,state FROM lead.tasks WHERE id=%s", (dependency.task_id,))
        if not row:
            raise ValueError("dependency_scope")
        # Only pre-existing tasks can be referenced; forward references cannot form a cycle.
        if row["state"] in ("failed", "canceled"):
            raise ValueError("dependency_failed")


def submit_work(c, aid, spec):
    if str(one(c,"SELECT lead.ctx_agent() id")["id"]) != str(aid):
        raise ValueError("explicit_agent_scope_required")
    spec = TaskSpec.model_validate(spec)
    validate_task(c, spec)
    intent = spec.model_dump(mode="json")
    material = {k: v for k, v in intent.items() if k != "review_at"}
    if spec.kind=="research" and spec.payload.get("document_ids") and not spec.payload.get("urls") and not spec.payload.get("queries"):
        material = {"kind":"source_inspection","documents":sorted(spec.payload["document_ids"]),
            "goal_revision":objective_revision(require_agent(c,aid))}
    if spec.kind=="channel":
        agent = require_agent(c,aid)
        operation = spec.payload.get('operation','publish')
        parent = spec.payload.get('parent_url') if operation == 'reply' else None
        prior = one(c,"""SELECT t.id,t.input FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id
            WHERE a.target_id=%s AND a.state IN ('dispatching','unknown','confirmed')
            AND coalesce(t.input#>>'{spec,payload,content_id}',a.content_id::text)=%s
            AND a.type=%s AND (CASE WHEN a.type='reply' THEN
                coalesce(a.intent->>'parent_url',t.input#>>'{spec,payload,parent_url}') END) IS NOT DISTINCT FROM %s
            ORDER BY a.created_at LIMIT 1""",(spec.target_id,str(spec.payload.get("content_id")),operation,parent))
        if prior:
            return TaskRef(agent_id=UUID(str(aid)),task_id=prior["id"],
                task_generation=prior["input"].get("task_generation",1)).model_dump(mode="json")
        binding = one(c,"""SELECT s.binding_version,i.revision intent_revision,r.generation route_generation
            FROM lead.social_accounts s LEFT JOIN lead.account_operating_intents i ON i.account_id=s.id
            LEFT JOIN lead.profile_execution_routes r ON r.profile_id=s.profile_id WHERE s.id=%s""",(spec.account_id,))
        material = {"kind":"channel","account_id":intent["account_id"],"target_id":intent["target_id"],
            "goal_revision":objective_revision(agent),"content":spec.payload.get("content_id"),
            "content_hash":spec.payload.get("content_hash"),"original_action_id":spec.payload.get("original_action_id"),
            "binding":binding}
        if not spec.payload.get('original_action_id'):
            # Old planned tasks used this hash without a primitive or parent.
            # Reuse only matching historical intent; never split it on upgrade.
            legacy = one(c,'SELECT id,input FROM lead.tasks WHERE workflow_id=%s',('growth:'+str(aid)+':'+digest(material),))
            previous = (legacy or {}).get('input',{}).get('spec',{}).get('payload',{})
            previous_operation = previous.get('operation','publish')
            previous_parent = previous.get('parent_url') if previous_operation == 'reply' else None
            if legacy and (previous_operation,previous_parent) == (operation,parent):
                return TaskRef(agent_id=UUID(str(aid)),task_id=legacy['id'],
                    task_generation=legacy['input'].get('task_generation',1)).model_dump(mode='json')
            material.update(operation=operation,parent_url=parent)
    workflow_id = "growth:" + str(aid) + ":" + digest(material)
    old = one(c, "SELECT id,input FROM lead.tasks WHERE workflow_id=%s", (workflow_id,))
    if old:
        return TaskRef(agent_id=UUID(str(aid)), task_id=old["id"],
                       task_generation=old["input"].get("task_generation", 1)).model_dump(mode="json")
    account = one(c, "SELECT binding_version,profile_id FROM lead.social_accounts WHERE id=%s", (spec.account_id,)) if spec.account_id else None
    agent = require_agent(c, aid)
    business = one(c,"SELECT id FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
    task = one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,experiment_id)
        VALUES(%s,%s,%s,'planned',%s,%s,%s,now(),%s)
        ON CONFLICT(workflow_id) DO NOTHING RETURNING id,input""",
        (current_tenant(), aid, "growth." + spec.kind, workflow_id,
         j({"schema_version": 1, "spec": intent, "task_generation": 1,
            "goal_revision": objective_revision(agent),
            "business_profile_id":str(business["id"]) if business else None,
            "binding_version": account["binding_version"] if account else None,
            "profile_id": str(account["profile_id"]) if account else None}),
         j({"runtime": {}, "output": {}}), spec.experiment_id))
    if not task:
        old = one(c, "SELECT id,input FROM lead.tasks WHERE workflow_id=%s", (workflow_id,))
        return TaskRef(agent_id=UUID(str(aid)), task_id=old["id"],
                       task_generation=old["input"].get("task_generation", 1)).model_dump(mode="json")
    ref = TaskRef(agent_id=UUID(str(aid)), task_id=task["id"]).model_dump(mode="json")
    emit(c, aid, "growth.work", ref)
    return ref


def create_experiment(c, aid, data):
    spec = ExperimentSpec.model_validate(data)
    validate_refs(c, spec.evidence_refs)
    agent = require_agent(c, aid)
    # Measurement semantics are owner-owned and frozen before any experiment work.
    from .outcomes import frozen_goal
    goal = frozen_goal(c, agent)
    if spec.activation_ref is not None and spec.activation_ref.model_dump(mode="json") != goal:
        raise ValueError("objective_definition_changed")
    spec = spec.model_copy(update={"activation_ref": GoalRef.model_validate(goal) if goal else None})
    business = one(c, "SELECT id FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
    # Variations and nested tasks share a problem budget; a model cannot mint a free root by renaming it.
    question_key = digest([str(aid), objective_revision(agent), spec.question_type])
    from ..admin_settings import effective
    from ..budget import available
    settings, _ = effective(c, "models", aid)
    budget = available(c, aid)["available_units"]
    ceiling = 4*2*(settings["max_output_tokens"]+60000//2+1000)
    if budget is not None:
        ceiling = min(ceiling,budget)
    if ceiling <= 0:
        raise ValueError("working_budget_empty")
    spec = spec.model_copy(update={"token_limit":min(spec.token_limit,ceiling)})
    exhausted = one(c, """SELECT count(*) n,max(closed_at) last_review FROM lead.growth_experiments
        WHERE question_key=%s AND state='closed' AND conclusion->>'verified_progress'='false'""", (question_key,))
    if exhausted["n"] >= 2:
        fresh = False
        for ref in spec.evidence_refs:
            if ref.kind in ("business","document","event"):
                table = {"business":"business_profiles","document":"knowledge_documents","event":"pixel_receipts"}[ref.kind]
                timestamp="received_at" if ref.kind=="event" else "created_at"
                fact = one(c,sql.SQL("SELECT {} AS created_at FROM lead.{} WHERE id=%s").format(sql.Identifier(timestamp),sql.Identifier(table)),(ref.id,))
                fresh = fresh or bool(fact and fact["created_at"]>exhausted["last_review"])
        if not fresh:
            raise ValueError("no_progress_path_exhausted")
    if spec.parent_experiment_id and not one(c, "SELECT id FROM lead.growth_experiments WHERE id=%s", (spec.parent_experiment_id,)):
        raise ValueError("experiment_parent_scope")
    variation_key = digest([spec.audience,spec.variation])
    existing = one(c,"SELECT id FROM lead.growth_experiments WHERE question_key=%s AND variation_key=%s",(question_key,variation_key))
    if not existing:
        if one(c,"SELECT count(*) n FROM lead.growth_experiments WHERE state IN ('active','observing')")["n"]>=8:
            raise ValueError("active_experiment_capacity")
        costs = question_costs(c,question_key)
        if costs["limit_units"] and costs["committed_units"]>=costs["limit_units"]:
            raise ValueError("question_limit")
    row = one(c, """INSERT INTO lead.growth_experiments(tenant_id,agent_id,business_profile_id,
        objective_revision,question_key,variation_key,specification)
        VALUES(%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(tenant_id,agent_id,question_key,objective_revision,variation_key)
        DO UPDATE SET question_key=EXCLUDED.question_key RETURNING *""",
        (current_tenant(), aid, business["id"] if business else None, objective_revision(agent),
         question_key, variation_key, j(spec.model_dump(mode="json"))))
    return row


def question_costs(c, question_key):
    limit = one(c, """SELECT min((specification->>'token_limit')::bigint) n FROM lead.growth_experiments
        WHERE question_key=%s""", (question_key,))["n"]
    costs = one(c, """SELECT coalesce(sum(CASE WHEN r.state IN ('reserved','unknown') THEN r.units ELSE 0 END),0)
        + coalesce(sum(u.units),0) committed_units FROM lead.budget_reservations r
        LEFT JOIN (SELECT reservation_id,sum(units) units FROM lead.usage_events GROUP BY reservation_id) u ON u.reservation_id=r.id
        WHERE r.pricing_snapshot#>>'{growth,question_key}'=%s""", (question_key,))
    return {"limit_units": int(limit or 0), "committed_units": int(costs["committed_units"])}


def experiment_progress(c,experiment_id):
    # Drafts, paraphrases and a second post are operational output. They do not
    # establish new demand, distribution or product-value evidence at review.
    return one(c,"""SELECT EXISTS(SELECT FROM lead.tasks WHERE experiment_id=%s AND state='succeeded' AND (
        (type='growth.research' AND result#>>'{output,output,learning_progress}'='true') OR
        (type='growth.channel' AND result#>>'{output,output,first_format_verified}'='true') OR
        (type='growth.observe' AND result#>>'{output,output,business_progress}'='true'))) ok""",(experiment_id,))["ok"]


def _check_command(c, agent, command):
    if command.goal_ref.revision != objective_revision(agent):
        raise ValueError("objective_changed")
    validate_refs(c, command.evidence_refs)
    if command.account_id:
        account = one(c, "SELECT * FROM lead.social_accounts WHERE id=%s", (command.account_id,))
        if not account or (command.expected_binding is not None and account["binding_version"] != command.expected_binding):
            raise ValueError("account_changed")
        intent = one(c, "SELECT desired FROM lead.account_operating_intents WHERE account_id=%s", (command.account_id,))
        if intent and intent["desired"] != "running":
            raise ValueError("account_paused")


def accept_commands(aid, proposal, decision_task_id=None,*,business_snapshot=None,decision_claim=None):
    proposal = DecisionProposal.model_validate(proposal)
    accepted, rejected = [], []
    groups = defaultdict(list)
    for command in proposal.commands:
        key = command_group(command)
        groups[key].append(command)
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        if decision_task_id:
            from datetime import datetime

            from .contracts import utcnow
            decision=one(c,"SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE",(decision_task_id,))
            lease=(decision or {}).get("result",{}).get("runtime",{})
            ref=(decision_claim or {}).get("ref",{})
            if (not decision or decision["type"]!="growth.decision" or decision["state"]!="running"
                or str(ref.get("agent_id"))!=str(aid) or str(ref.get("task_id"))!=str(decision_task_id)
                or ref.get("task_generation")!=decision["input"]["task_generation"]
                or (decision_claim or {}).get("attempt_token")!=lease.get("attempt_token")
                or not lease.get("lease_until") or datetime.fromisoformat(lease["lease_until"])<=utcnow()):
                return {"decision_task_id":decision_task_id,"accepted":[],"rejected":[],"discarded":"stale_decision"}
        if agent["desired_state"] in ("paused", "archived"):
            return {"decision_task_id": decision_task_id, "accepted": [], "rejected": [
                {"group": key, "reason": "project_paused"} for key in groups]}
        if business_snapshot is not None:
            actual = one(c,"SELECT id FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
            if (str(actual["id"]) if actual else None)!=business_snapshot["id"]:
                return {"decision_task_id":decision_task_id,"accepted":[],"discarded":"product_changed","rejected":[
                    {"group":key,"reason":"product_changed"} for key in groups]}
        for key, commands in groups.items():
            try:
                with c.transaction():
                    request_key = digest([str(aid), key, [command.command_key for command in commands]])
                    request_hash = digest([command.model_dump(mode="json") for command in commands])
                    request = one(c, """INSERT INTO lead.idempotency_requests(tenant_id,agent_id,actor_ref,scope,
                        idempotency_key,request_hash,state,expires_at) VALUES(%s,%s,'growth','growth.command',%s,%s,
                        'pending',now()+interval '90 days') ON CONFLICT DO NOTHING RETURNING id""",
                        (current_tenant(), aid, request_key, request_hash))
                    if not request:
                        previous = one(c, """SELECT request_hash,response_ref FROM lead.idempotency_requests
                            WHERE actor_ref='growth' AND scope='growth.command' AND idempotency_key=%s""", (request_key,))
                        if previous["request_hash"] != request_hash:
                            raise ValueError("command_key_conflict")
                        accepted.append(json.loads(previous["response_ref"]))
                        continue
                    refs, experiment = [], None
                    for command in commands:
                        _check_command(c, agent, command)
                    # Allocate experiment IDs before tasks regardless of model command ordering.
                    for command in commands:
                        if command.kind == "experiment.create":
                            if experiment:
                                raise ValueError("multiple_experiments_in_group")
                            experiment = create_experiment(c, aid, command.payload)
                    for command in commands:
                        if command.kind == "task.create":
                            payload = dict(command.payload)
                            if one(c,"""SELECT count(*) n FROM lead.tasks WHERE type LIKE 'growth.%%'
                                AND type NOT IN ('growth.account','growth.observe','growth.recover')
                                AND state IN ('planned','queued','running','waiting_user')""")["n"]>=64:
                                raise ValueError("work_capacity")
                            if payload.get("kind") == "decision" or (payload.get("kind") == "account" and payload.get("purpose") == "connect"):
                                raise ValueError("owner_command_required")
                            if payload.get("kind")=="channel" and payload.get("payload",{}).get("original_action_id"):
                                raise ValueError("runtime_reconciliation_only")
                            if payload.get("kind")=="channel" and (not command.account_id or command.expected_binding is None
                                    or payload.get("payload",{}).get("publication_purpose")=="diagnostic"):
                                raise ValueError("business_account_snapshot_required")
                            if experiment:
                                payload["experiment_id"] = str(experiment["id"])
                            if payload.get("kind") in ("research","content","channel") and not payload.get("experiment_id"):
                                raise ValueError("bounded_experiment_required")
                            if command.experiment_id and str(payload.get("experiment_id")) != str(command.experiment_id):
                                raise ValueError("experiment_scope")
                            if command.account_id:
                                if payload.get("account_id") and str(payload["account_id"]) != str(command.account_id):
                                    raise ValueError("account_scope")
                                payload["account_id"] = str(command.account_id)
                            refs.append(submit_work(c, aid, payload))
                        elif command.kind in ("experiment.close", "experiment.revise"):
                            row = one(c, "SELECT * FROM lead.growth_experiments WHERE id=%s FOR UPDATE",
                                      (command.experiment_id,))
                            if not row or row["revision"] != command.expected_revision:
                                raise ValueError("experiment_changed")
                            if row["state"] not in ("active","observing"):
                                raise ValueError("experiment_closed")
                            if command.kind == "experiment.close":
                                progress = experiment_progress(c,row["id"])
                                c.execute("""UPDATE lead.growth_experiments SET state='closed',revision=revision+1,
                                    conclusion=%s,closed_at=now(),updated_at=now() WHERE id=%s""",
                                    (j({**command.payload,"verified_progress":progress}), row["id"]))
                                c.execute("""UPDATE lead.tasks SET state='canceled',completed_at=now()
                                    WHERE experiment_id=%s AND state IN ('planned','queued','waiting_user')
                                    AND NOT EXISTS(SELECT FROM lead.actions a WHERE a.task_id=lead.tasks.id
                                        AND a.state IN ('dispatching','unknown','confirmed'))""", (row["id"],))
                            else:
                                spec = ExperimentSpec.model_validate(command.payload)
                                frozen = row["specification"]
                                if spec.activation_ref and spec.activation_ref.model_dump(mode="json") != frozen.get("activation_ref"):
                                    raise ValueError("experiment_definition_immutable")
                                spec = spec.model_copy(update={"activation_ref": GoalRef.model_validate(frozen["activation_ref"]) if frozen.get("activation_ref") else None,
                                    "token_limit":min(spec.token_limit,frozen["token_limit"]),
                                    "review_at":min(spec.review_at,ExperimentSpec.model_validate(frozen).review_at)})
                                c.execute("UPDATE lead.growth_experiments SET specification=%s,revision=revision+1,updated_at=now() WHERE id=%s",
                                          (j(spec.model_dump(mode="json")), row["id"]))
                        elif command.kind == "target.propose":
                            if not command.account_id:
                                raise ValueError("account_scope")
                            from ..adapters.catalog import target_url
                            from .tools import AccountInput
                            account = one(c,"""SELECT a.platform,a.connection_origin,p.provider FROM lead.social_accounts a
                                LEFT JOIN lead.browser_profiles p ON p.id=a.profile_id WHERE a.id=%s""",(command.account_id,))
                            proposed = AccountInput.model_validate(command.payload)
                            if account['platform']=='web' and account.get('provider')=='chromium':
                                from ..browser_execution import origin
                                urls=list(proposed.target_urls)
                                if any(not 1<=len(url)<=2000 or origin(url)!=origin(account['connection_origin']) for url in urls):
                                    raise ValueError('target_not_permitted')
                            else:
                                urls = [target_url(account["platform"],url) for url in proposed.target_urls]
                            if not urls:
                                raise ValueError("target_candidates_required")
                            from datetime import timedelta

                            from .contracts import utcnow
                            refs.append(submit_work(c,aid,TaskSpec(kind="account",purpose="discover",account_id=command.account_id,
                                payload={"target_urls":urls,**({'desired_operation':proposed.desired_operation}
                                    if proposed.desired_operation is not None else {})},review_at=utcnow()+timedelta(hours=2))))
                            audit(c, aid, "growth.target_proposed", command.payload)
                    response = {"group": key, "task_refs": refs,
                                "experiment_id": str(experiment["id"]) if experiment else None}
                    c.execute("UPDATE lead.idempotency_requests SET state='complete',response_ref=%s,http_status=200 WHERE id=%s",
                              (json.dumps(response), request["id"]))
                    accepted.append(response)
            except (ValueError, ValidationError, DataError, IntegrityError) as error:
                reason = "invalid_scoped_reference" if isinstance(error,(DataError,IntegrityError)) else str(error)[:200]
                rejected.append({"group": key, "reason": reason})
        audit(c, aid, "growth.decision_accepted", {"decision_task_id": decision_task_id,
                                               "accepted": accepted, "rejected": rejected})
    return {"decision_task_id": decision_task_id, "accepted": accepted, "rejected": rejected}
