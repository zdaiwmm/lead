"""Operator-only, scoped ownership transfer. Existing operation identities and fees survive."""
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row

from ..account_intents import auth_scope
from ..config import database_kwargs
from ..db import all_rows, audit, emit, j, one, tx
from ..identity_store import agent_tenant
from ..scope import tenant_scope, trusted_execution
from .contracts import TaskSpec, utcnow
from .store import digest, enabled, submit_work


def bootstrap(c,aid):
    c.execute("SELECT set_config('app.agent_id',%s,true)",(str(aid),))
    agent = one(c,"SELECT * FROM lead.agents WHERE id=%s",(aid,))
    if not agent or not enabled(agent) or agent["desired_state"]=="archived":
        return None
    business = one(c,"SELECT * FROM lead.business_profiles WHERE agent_id=%s AND tenant_id=lead.ctx_tenant() ORDER BY revision DESC LIMIT 1",(aid,))
    if not business or not (business["product_url"] or business["description"].strip()):
        return None
    runtime = dict(agent["onboarding_state"].get("growth_runtime") or {})
    paused = runtime.get("global_control",{}).get("source") in ("owner_command","verified_migration") and agent["desired_state"]=="paused"
    if not paused and not agent["onboarding_state"].get("home_release_maintenance"):
        c.execute("""UPDATE lead.agents SET desired_state='running',health_state='queued',
            onboarding_state=onboarding_state || '{"setup_pending":false}'::jsonb WHERE id=%s""",(aid,))
    ref = submit_work(c,aid,TaskSpec(kind="research",purpose="product",priority="normal",
        review_at=utcnow()+timedelta(hours=2),dedupe_key="product:"+str(business["id"])+":"+str(agent["control_version"]),
        evidence_refs=[{"kind":"business","id":business["id"],"revision":business["revision"]}]))
    return ref


def _snapshot(c,aid):
    agent = one(c,"SELECT * FROM lead.agents WHERE id=%s AND tenant_id=lead.ctx_tenant()",(aid,))
    if not agent:
        raise LookupError("project_not_found")
    accounts = all_rows(c,"""SELECT s.id,s.profile_id,s.platform,s.binding_version,s.state,s.automation_enabled,
        s.verified_at,s.external_account_id,s.created_at FROM lead.social_accounts s WHERE s.agent_id=%s AND s.tenant_id=lead.ctx_tenant() ORDER BY s.id""",(aid,))
    grants = all_rows(c,"""SELECT g.id,g.target_id,g.allowed_actions,g.limits,g.granted_by,g.granted_at,g.revoked_at,
        t.account_id,t.external_target_id FROM lead.authorization_grants g JOIN lead.account_targets t ON t.id=g.target_id
        WHERE g.revoked_at IS NULL AND g.agent_id=%s AND g.tenant_id=lead.ctx_tenant() ORDER BY g.id""",(aid,))
    routes = all_rows(c,"SELECT * FROM lead.profile_execution_routes WHERE agent_id=%s AND tenant_id=lead.ctx_tenant() ORDER BY profile_id",(aid,))
    tasks = all_rows(c,"""SELECT id,type,state,version,input,result FROM lead.tasks WHERE type NOT LIKE 'growth.%%'
        AND state IN ('planned','queued','running','waiting_user','reconciling') AND agent_id=%s AND tenant_id=lead.ctx_tenant() ORDER BY id""",(aid,))
    actions = all_rows(c,"""SELECT a.id,a.task_id,a.target_id,a.content_id,a.state,a.operation_key,a.intent,
        t.account_id FROM lead.actions a JOIN lead.account_targets t ON t.id=a.target_id
        WHERE a.state IN ('prepared','dispatching','unknown') AND a.agent_id=%s AND a.tenant_id=lead.ctx_tenant() ORDER BY a.id""",(aid,))
    profile_ids = {str(account["profile_id"]) for account in accounts}
    missing = sorted(profile_ids-{str(route["profile_id"]) for route in routes})
    native = [str(task["id"]) for task in tasks if
        (task["result"] or {}).get("reply_state") in ("dispatching","unknown") or
        (task["result"] or {}).get("join_state") in ("dispatching","unknown","pending_approval")]
    intent_sources=[]
    for account in accounts:
        owner = one(c,"""SELECT id,type,occurred_at FROM lead.audit_events WHERE agent_id=%s AND tenant_id=lead.ctx_tenant()
            AND type IN ('channel.paused','channel.resumed') AND actor_type IN ('oidc','local_owner')
            AND payload->>'platform'=%s AND (payload->>'account_id' IS NULL OR payload->>'account_id'=%s)
            AND occurred_at>=%s ORDER BY occurred_at DESC,id DESC LIMIT 1""",(aid,account["platform"],str(account["id"]),account["created_at"]))
        excluded=all_rows(c,"""SELECT t.external_target_id FROM lead.account_targets t WHERE t.account_id=%s
            AND t.agent_id=%s AND t.tenant_id=lead.ctx_tenant() AND EXISTS(SELECT FROM lead.authorization_grants g
                WHERE g.target_id=t.id AND g.revoked_at IS NOT NULL)
            AND NOT EXISTS(SELECT FROM lead.authorization_grants g WHERE g.target_id=t.id AND g.revoked_at IS NULL)""",(account["id"],aid))
        desired = "revoked" if account["state"]=="disconnected" else "paused" if owner and owner["type"]=="channel.paused" else (
            "running" if account["verified_at"] and account["external_account_id"] else None)
        intent_sources.append({"account_id":str(account["id"]),"desired":desired,"owner_event":owner,
            "connection_verified_at":account["verified_at"],"excluded_targets":[item["external_target_id"] for item in excluded]})
    material = {"agent_id":str(aid),"control_version":agent["control_version"],"desired_state":agent["desired_state"],
        "config_revision":agent["config_revision"],"generation":agent["onboarding_state"].get("growth_runtime",{}).get("decision",{}).get("generation",0),
        "accounts":accounts,"grants":grants,"routes":routes,
        "legacy_tasks":[{"id":task["id"],"type":task["type"],"state":task["state"],"version":task["version"],
                         "intent_hash":digest(task["input"]),"result_hash":digest(task["result"])} for task in tasks],
        "actions":[{key:value for key,value in action.items() if key!="intent"} | {"intent_hash":digest(action["intent"])} for action in actions],
        "missing_routes":missing,"native_receipt_tasks":native,"intent_sources":intent_sources}
    return agent,accounts,grants,routes,tasks,actions,material


def preview_migration(aid):
    with tx(aid) as c:
        *_,report = _snapshot(c,aid)
    return {**report,"report_hash":digest(report)}


def apply_migration(aid,report_hash,expected_generation):
    if not trusted_execution():
        raise PermissionError("operator_migration_only")
    with tenant_scope(agent_tenant(aid),None), psycopg.connect(**database_kwargs(owner=True),row_factory=dict_row) as c:
        auth_scope(c,aid)
        c.execute("SELECT set_config('app.growth_protocol','1',true)")
        one(c,"SELECT id FROM lead.agents WHERE id=%s FOR UPDATE",(aid,))
        agent,accounts,grants,routes,tasks,actions,report = _snapshot(c,aid)
        state = dict(agent["onboarding_state"])
        prior = state.get("growth_runtime",{})
        if prior.get("migration",{}).get("report_hash")==report_hash:
            return {"applied":True,"duplicate":True,"generation":prior["decision"]["generation"]}
        if report["generation"] != expected_generation or digest(report) != report_hash:
            raise ValueError("migration_snapshot_changed")
        generation = expected_generation+1
        # Translate only source-backed existing criteria. A historical click or
        # an owner sentence alone never manufactures a trusted value event.
        contract = dict(state.get("operating_contract") or {})
        if not contract.get("event_ref") and contract.get("source_message_id"):
            source = one(c,"SELECT id FROM lead.conversation_messages WHERE id=%s AND agent_id=%s AND role='user'",
                         (contract["source_message_id"],aid))
            event_name = {"purchase":"Purchase","signup":"CompleteRegistration","lead":"Lead","trial":"StartTrial"}.get(contract.get("objective_event"))
            event = one(c,"""SELECT d.connection_id,d.name,d.revision FROM lead.pixel_definitions d
                JOIN lead.analytics_connections a ON a.id=d.connection_id WHERE d.agent_id=%s AND d.method='server'
                AND d.name=%s AND a.state<>'disconnected' ORDER BY d.updated_at DESC LIMIT 1""",(aid,event_name)) if source and event_name else None
            if event:
                from .contracts import GoalRef
                contract["event_ref"] = GoalRef(connection_id=event["connection_id"],event_name=event["name"],
                    definition_revision=event["revision"],revision=max(1,contract.get("revision",1))).model_dump(mode="json")
                contract["definition_source"]={"kind":"verified_migration","source_ref":report_hash,
                    "original_message_id":contract["source_message_id"],"original_objective_event":contract["objective_event"]}
                state["operating_contract"]=contract
        old_policy = state.get("growth_trial_policy") or {}
        if (not state.get("growth_budget_policy") and state.get("daily_token_limit") is None
            and old_policy.get("mode")=="active" and old_policy.get("focus")=="chamber_first_users"):
            state["growth_budget_policy"]={"mode":"unlimited","source_kind":"verified_migration",
                "source_ref":"growth-migration:"+report_hash,"previous_policy":old_policy}
        for account in accounts:
            if one(c,"SELECT account_id FROM lead.account_operating_intents WHERE account_id=%s",(account["id"],)):
                continue
            source=next(item for item in report["intent_sources"] if item["account_id"]==str(account["id"]))
            if source["desired"] is None:
                # An old connected/false flag without identity proof or an
                # explicit owner event is not a pause. Real login completion
                # will create the first canonical intent automatically.
                continue
            owned = [grant for grant in grants if grant["account_id"]==account["id"]]
            limits = [grant["limits"].get("publish_per_day") for grant in owned if grant["limits"].get("publish_per_day") is not None]
            scope = {"target_scope":"existing_grants" if owned else "owned_managed","target_ids":[grant["external_target_id"] for grant in owned],
                "excluded_targets":source["excluded_targets"],"source_grants":[str(grant["id"]) for grant in owned],
                "source_connection_verified_at":source["connection_verified_at"].isoformat() if source["connection_verified_at"] else None,
                "source_owner_event":str(source["owner_event"]["id"]) if source["owner_event"] else None}
            if limits:
                scope["publish_per_day"]=min(limits)
            desired = source["desired"]
            c.execute("SELECT lead.growth_set_intent(%s,%s,%s,'verified_migration',%s,NULL,%s)",
                      (aid,account["id"],desired,"growth-migration:"+report_hash,j(scope)))
        uncertain = {str(action["task_id"]):action for action in actions if action["state"] in ("dispatching","unknown")}
        for task in tasks:
            original = uncertain.get(str(task["id"]))
            if str(task["id"]) in report["native_receipt_tasks"]:
                account = next((a for a in accounts if str(a["id"])==task["input"].get("account_id") or (
                    a["platform"]=="telegram" and a["external_account_id"]==task["input"].get("external_account_id"))),None)
                if not account:
                    # No replacement identity is inferred. Preserve the exact old
                    # work and expose the local missing binding for reconciliation.
                    profile_id = task["input"].get("channel_profile_id") or task["input"].get("profile_id")
                else:
                    profile_id = str(account["profile_id"])
                c.execute("UPDATE lead.tasks SET input=input || %s WHERE id=%s",(j({"profile_id":profile_id,
                    "migration":{"owner":"receipt_only","generation":generation,
                        "original_account_id":str(account["id"]) if account else None}}),task["id"]))
                submit_work(c,aid,TaskSpec(kind="observe",purpose="original_native_receipt",
                    account_id=account["id"] if account else None,payload={"legacy_native_task_id":str(task["id"])},
                    review_at=utcnow()+timedelta(days=30),dedupe_key="original-native:"+str(task["id"])))
                continue
            if original:
                spec = TaskSpec(kind="channel",purpose="reconcile_original",account_id=original["account_id"],
                    target_id=original["target_id"],payload={"original_action_id":str(original["id"])},
                    review_at=utcnow()+timedelta(days=30),dedupe_key="original:"+str(original["id"]))
                account = next(a for a in accounts if a["id"]==original["account_id"])
                route = next((route for route in routes if route["profile_id"]==account["profile_id"]),None)
                original_execution = {"profile_id":str(route["profile_id"]),"executor_ref":route["executor_ref"],
                    "transport_ref":route["transport_ref"],"route_generation":route["generation"]} if route else None
                updated = {**task["input"],"spec":spec.model_dump(mode="json"),"task_generation":1,
                    "profile_id":str(account["profile_id"]),"binding_version":account["binding_version"],"migration":{"generation":generation,"original_type":task["type"]}}
                updated["migration"]["original_execution_ref"] = original_execution
                c.execute("UPDATE lead.tasks SET type='growth.channel',state='reconciling',input=%s,result=%s,scheduled_at=now() WHERE id=%s",
                    (j(updated),j({"runtime":{"operation_ref":str(original["id"]),"attempts":0},"legacy_output":task["result"],"output":{}}),task["id"]))
                emit(c,aid,"growth.work",{"agent_id":str(aid),"task_id":str(task["id"]),"task_generation":1})
            else:
                c.execute("UPDATE lead.tasks SET state='canceled',completed_at=now(),result=coalesce(result,'{}') || %s WHERE id=%s",
                          (j({"migration":{"generation":generation,"reason":"new_runtime_owns_business"}}),task["id"]))
        c.execute("UPDATE lead.actions SET state='canceled' WHERE state='prepared' AND agent_id=%s AND tenant_id=lead.ctx_tenant()",(aid,))
        state["growth_runtime"]={"decision":{"engine":"growth","generation":generation},
            "global_control":{"source":"verified_migration","desired":"running" if state.get("home_release_maintenance") else agent["desired_state"]},
            "migration":{"report_hash":report_hash,"generation":generation,"missing_routes":report["missing_routes"]}}
        # Historical plans stay queryable in the database; they stop being current inputs.
        for name in ("plan_focus","plan_preferences_revision","operating_kernel","channel_clocks","managed_channel_delivery"):
            state.pop(name,None)
        # Engine generation fences ownership. This technical transfer does not change user controls.
        c.execute("UPDATE lead.agents SET onboarding_state=%s,updated_at=now() WHERE id=%s",(j(state),aid))
        audit(c,aid,"growth.migration_applied",{"report_hash":report_hash,"generation":generation,
            "original_actions":len(uncertain),"missing_routes":report["missing_routes"]})
        if agent["desired_state"] in ("running","draft") or state.get("home_release_maintenance"):
            bootstrap(c,aid)
        return {"applied":True,"generation":generation,"original_actions":len(uncertain),"missing_routes":report["missing_routes"]}
