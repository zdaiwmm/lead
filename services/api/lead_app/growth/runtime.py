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
    if state in ("failed", "canceled") and row["state"] not in TERMINAL and not runtime.get("episode_root"):
        reason = runtime.get("reason") or state
        notification = digest([state, reason])
        if runtime.get("terminal_notification") != notification:
            runtime["terminal_notification"] = notification
            emit(c, str(row["agent_id"]), "growth.wake", {"reason":reason, "task_id":str(row["id"]),
                 "transition":notification})
    value = {"runtime": runtime}
    if output is not None:
        value["output"] = output
    c.execute("""UPDATE lead.tasks SET state=%s,result=coalesce(result,'{}') || %s,version=version+1,
        scheduled_at=coalesce(%s,scheduled_at),completed_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s""",
        (state, j(value), when, state in TERMINAL, row["id"]))


def _wait(row, reason, now, *, producer="growth.runtime", action="recheck", delay=60, owner="product"):
    return Wait(reason=reason, producer=producer, recovery_action=action, check_at=now + timedelta(seconds=delay),
                episode_root=row["id"], responsibility=owner).model_dump(mode="json")


def _receipt_delay(runtime, action):
    progress = runtime.get('receipt_reconciliation') or {}
    return 3600 if (action and progress.get('action_id') == str(action['id'])
                    and progress.get('completed_checks', 0) >= 3) else 300


def _browser_resource(c, row, spec):
    profile = row['input'].get('profile_id') or spec.payload.get('profile_id')
    record = one(c, 'SELECT provider FROM lead.browser_profiles WHERE id=%s', (profile,)) if profile else None
    account = spec.account_id or spec.payload.get('connection_account_id')
    return profile, bool(record and record['provider'] == 'chromium'), account


def _native_session_condition(c, profile):
    return one(c, """SELECT l.epoch,b.id session_id,coalesce(b.state='ready',false) ready,
        coalesce(l.state='held' AND l.expires_at>now(),false) held
        FROM lead.profile_leases l LEFT JOIN lead.browser_sessions b
            ON b.profile_id=l.profile_id AND b.epoch=l.epoch AND b.ended_at IS NULL
        WHERE l.profile_id=%s""", (profile,))


def _resource_condition(c, agent, row, spec):
    """Conditions come from actual references/configuration, never a model-supplied retry key."""
    if spec.kind in ("content", "decision") or _runtime(row).get("failure_stage")=="model":
        from ..admin_settings import effective, task_connection
        settings, version = effective(c, "models", str(agent["id"]))
        from .agent import PROMPT_VERSION
        config,reference,_ = task_connection(c,str(agent["id"]),"planning" if spec.kind=="content" else "research")
        return "model:"+reference, digest([version, settings, PROMPT_VERSION, config["key"]])
    if spec.kind == "research" and _runtime(row).get("failure_scope"):
        return _current_research_resource(c, _runtime(row)["failure_scope"])
    if spec.account_id or spec.payload.get("profile_id"):
        profile, native, account_id = _browser_resource(c, row, spec)
        route = one(c, "SELECT generation FROM lead.profile_execution_routes WHERE profile_id=%s", (profile,))
        account = one(c,"""SELECT s.binding_version,s.state,s.verified_at,i.revision,i.desired,i.scope FROM lead.social_accounts s
            LEFT JOIN lead.account_operating_intents i ON i.account_id=s.id WHERE s.id=%s""",(account_id,)) if account_id else None
        capability = None
        if spec.kind=="channel":
            operation = spec.payload.get('operation', 'publish')
            capability = one(c,"""SELECT t.state,t.verified_permissions->>'adapter_version' adapter_version,
                coalesce(t.verified_permissions->'browser_methods'->%s,
                    t.verified_permissions->'browser_method') browser_method,
                g.id grant_id,g.version,g.allowed_actions,g.limits,pc.permission_state,pc.technical_state,
                pc.adapter_version installed_version FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id
                LEFT JOIN lead.platform_capabilities pc ON pc.platform=s.platform AND pc.target_type=t.target_type AND pc.action_type=%s
                LEFT JOIN LATERAL (SELECT * FROM lead.authorization_grants WHERE target_id=t.id AND revoked_at IS NULL ORDER BY version DESC LIMIT 1) g ON true
                WHERE t.id=%s""",(operation,operation,spec.target_id))
        resource = 'account:' + str(account_id) if native and account_id else 'profile:' + str(profile)
        connection = one(c, "SELECT id,challenge_version,control_mode,state,expires_at FROM lead.takeover_sessions WHERE id=%s",
                         (spec.payload['takeover_id'],)) if spec.payload.get('takeover_id') else None
        session = _native_session_condition(c, profile) if native else None
        return resource, digest([account,(route or {}).get("generation"),capability,connection,session])
    urls = spec.payload.get("urls") or []
    if not urls and spec.kind == "research":
        business = one(c, "SELECT product_url FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
        urls = [business["product_url"]] if business and business["product_url"] else []
    return spec.kind+":"+digest(sorted({urlsplit(url).hostname for url in urls})), digest(urls)


def _proposal_key(row):
    spec=row["input"].get("spec",{})
    return digest([row["type"],row["input"].get("business_profile_id"),row["input"].get("goal_revision"),
        [{key:value for key,value in trigger.items() if not key.startswith("_")}
            for trigger in spec.get("payload",{}).get("triggers",[])],spec.get("evidence_refs",[])])


def research_resource(scope):
    """A resource identifies the attempted capability, not the task or error spelling."""
    endpoint = scope.get("endpoint") or scope.get("origin") or "unknown"
    parsed = urlsplit(endpoint)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else endpoint
    location = endpoint.split("?", 1)[0] if scope.get("endpoint") else origin
    key = [scope.get("provider", "unknown"), scope.get("operation", "read"), location,
           scope.get("access_mode", "public"), str(scope.get("account_id") or "")]
    if "limiting_resource" in scope:
        # Local admission failures keep the actual consuming question, project
        # or TaskRef isolated; ordinary provider/source keys remain unchanged.
        key.append(scope["limiting_resource"])
    return "research:" + digest(key), digest([key, scope.get("condition_revision", "initial")])


def _current_research_resource(c, scope):
    resource, condition = research_resource(scope)
    if scope.get("provider") in ("tavily", "tikhub"):
        from ..research_providers import configuration_revision
        aid = one(c, "SELECT current_setting('app.agent_id',true) aid")["aid"]
        condition = digest([condition, configuration_revision(scope['provider'],scope['operation'],aid,c)])
    if scope.get("account_id"):
        account = one(c, """SELECT s.binding_version,s.state,s.verified_at,i.revision,i.desired,r.generation,
            s.profile_id,p.provider
            FROM lead.social_accounts s LEFT JOIN lead.account_operating_intents i ON i.account_id=s.id
            LEFT JOIN lead.profile_execution_routes r ON r.profile_id=s.profile_id
            LEFT JOIN lead.browser_profiles p ON p.id=s.profile_id WHERE s.id=%s""", (scope["account_id"],))
        session = _native_session_condition(c, account['profile_id']) if account and account['provider'] == 'chromium' else None
        condition = digest([condition, account, session])
    return resource, condition


def _recovery_lock(c, aid):
    # One short project transaction orders episode changes and proof consumption.
    # Never held during a model, browser or network operation.
    c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("growth-recovery:"+str(aid),))


def wake_connection(aid, connection_id, challenge_version):
    """A real owner transition wakes its original wait without granting recovery proof."""
    with tx(aid) as c:
        _recovery_lock(c, aid)
        require_agent(c, aid, True)
        connection = one(c, "SELECT * FROM lead.takeover_sessions WHERE id=%s AND purpose='login'", (connection_id,))
        if not connection or connection['challenge_version'] != challenge_version:
            return []
        now = utcnow()
        rows = all_rows(c, """SELECT * FROM lead.tasks WHERE type='growth.account'
            AND input#>>'{spec,payload,takeover_id}'=%s AND state NOT IN ('succeeded','failed','canceled')
            ORDER BY id FOR UPDATE""", (str(connection_id),))
        refs = []
        for row in rows:
            runtime = _runtime(row)
            if connection['state'] in ('ended','expired'):
                runtime.update(attempt_token=None, lease_until=None, wait=None, reason='connection_canceled')
                _save(c, row, 'canceled', runtime)
                continue
            wait = runtime.get('wait') or {}
            if (connection['state'] != 'active' or connection['control_mode'] != 'automatic'
                    or connection['expires_at'] <= now or row['state'] == 'running'
                    or wait.get('producer') != 'account_connection'):
                continue
            # Preserve attempts, costs, the original deadline and every blocked
            # dependency. A changed challenge is permission to recheck, no proof.
            runtime['wait'] = None
            _save(c, row, 'planned', runtime, when=now)
            ref = {'agent_id': str(aid), 'task_id': str(row['id']),
                   'task_generation': row['input']['task_generation']}
            emit(c, aid, 'growth.work', ref)
            refs.append(ref)
        return refs


def wake_browser_session(aid, profile_id, session_id, epoch):
    """Ready permits original nonterminal work to recheck; it proves no recovery."""
    with tx(aid) as c:
        _recovery_lock(c, aid)
        agent = require_agent(c, aid, True)
        all_rows(c, 'SELECT id FROM lead.social_accounts ORDER BY id FOR SHARE')
        # Match the actual lease -> session heartbeat order.
        lease = one(c, """SELECT epoch FROM lead.profile_leases WHERE profile_id=%s AND epoch=%s
            AND state='held' AND expires_at>now() FOR SHARE""", (profile_id, epoch))
        session = one(c, """SELECT b.id FROM lead.browser_sessions b JOIN lead.browser_profiles p ON p.id=b.profile_id
            WHERE b.id=%s AND b.profile_id=%s AND b.epoch=%s AND b.state='ready' AND b.ended_at IS NULL
                AND p.provider='chromium' FOR SHARE OF b""", (session_id, profile_id, epoch)) if lease else None
        if not session:
            return []
        rows = all_rows(c, """SELECT * FROM lead.tasks WHERE type LIKE 'growth.%%'
            AND state IN ('planned','queued','waiting_user','reconciling')
            AND (coalesce(input->>'profile_id',input#>>'{spec,payload,profile_id}')=%s
                OR coalesce(input#>>'{spec,account_id}',input#>>'{spec,payload,connection_account_id}',
                    result#>>'{runtime,failure_scope,account_id}') IN
                    (SELECT id::text FROM lead.social_accounts WHERE profile_id=%s))
            ORDER BY id FOR UPDATE""", (str(profile_id), profile_id))
        refs = []
        revision = digest([str(session_id), epoch])
        for row in rows:
            runtime = _runtime(row)
            wait = runtime.get('wait') or {}
            if (runtime.get('browser_session_wake') == revision or wait.get('recovery_action') == 'owner'
                    or wait.get('producer') in ('budget','home_release_maintenance')):
                continue
            action = one(c, 'SELECT state FROM lead.actions WHERE task_id=%s', (row['id'],))
            reconcile = bool(action and action['state'] in ('dispatching','unknown','confirmed'))
            if not reconcile and _resume_allowed(c, agent, row):
                continue
            resource, condition = _resource_condition(c, agent, row, TaskSpec.model_validate(row['input']['spec']))
            if not runtime.get('condition_revision') or condition == runtime['condition_revision']:
                continue
            # Leave every dependency and recovery proof intact. Admission and
            # the actual browser still revalidate current authority/identity.
            runtime.update(wait=None, browser_session_wake=revision)
            _save(c, row, 'reconciling' if reconcile else 'planned', runtime, when=utcnow())
            ref = {'agent_id': str(aid), 'task_id': str(row['id']),
                   'task_generation': row['input']['task_generation']}
            emit(c, aid, 'growth.work', ref)
            refs.append(ref)
        return refs


def wake_browser_method(aid, target_id, operation):
    """Consume a committed, current producer fact for its untouched original work."""
    from ..browser_method_recovery import method_repair_candidate
    if operation not in ('publish', 'reply'):
        return []
    with tx(aid) as c:
        _recovery_lock(c, aid)
        agent = require_agent(c, aid, True)
        all_rows(c, 'SELECT id FROM lead.social_accounts ORDER BY id FOR SHARE')
        all_rows(c, 'SELECT id FROM lead.authorization_grants ORDER BY id FOR SHARE')
        rows = all_rows(c, """SELECT t.* FROM lead.tasks t JOIN lead.actions a ON a.task_id=t.id
            WHERE a.target_id=%s AND a.type=%s AND a.state='prepared' AND t.type='growth.channel'
              AND t.state IN ('failed','planned','queued','waiting_user') ORDER BY t.id FOR UPDATE OF t""",
            (target_id, operation))
        refs = []
        for row in rows:
            runtime = _runtime(row)
            wait = runtime.get('wait') or {}
            if (runtime.get('reason') in PERMANENT or _resume_allowed(c, agent, row)
                    or wait.get('recovery_action') == 'owner'
                    or wait.get('producer') in ('budget', 'home_release_maintenance')):
                continue
            action = one(c, 'SELECT * FROM lead.actions WHERE task_id=%s FOR UPDATE', (row['id'],))
            candidate = method_repair_candidate(c, action)
            if not candidate:
                continue
            previous = runtime.get('browser_method_repair') or {}
            if (previous.get('method_hash') == candidate['method_hash']
                    and previous.get('browser_session_id') == candidate['browser_session_id']
                    and previous.get('browser_epoch') == candidate['browser_epoch']
                    and previous.get('grant_id') == str(action['grant_id'])):
                continue
            resource, condition = _resource_condition(c, agent, row, TaskSpec.model_validate(row['input']['spec']))
            if resource != runtime.get('resource_key') or condition == runtime.get('condition_revision'):
                continue
            episode = dict(runtime.get('recovery') or {'resource': resource,
                'condition_revision': runtime['condition_revision']})
            if episode.get('resource') != resource or episode.get('condition_revision') != runtime.get('condition_revision'):
                continue
            proof = candidate['proof_ref']
            marker = {'target_id': str(target_id), 'operation': operation, 'method_hash': candidate['method_hash'],
                'grant_id': str(action['grant_id']), 'proof_ref': proof,
                'browser_session_id': candidate['browser_session_id'], 'browser_epoch': candidate['browser_epoch'],
                'producer_task_id': candidate['producer_task_id'],
                'task_generation': row['input']['task_generation'] + (row['state'] == 'failed')}
            if row['state'] == 'failed':
                episode.update(status='recovered', proof_ref=proof, observed_condition=condition)
                current = {**row, 'result': {**(row['result'] or {}),
                    'runtime': {**runtime, 'browser_method_repair': marker}}}
                ref = _resume_proven(c, aid, agent, current, episode, proof)
                if ref:
                    refs.append(ref)
                continue
            proof_key = digest([resource, episode['condition_revision'], proof])
            consumed = list(runtime.get('recovery_proofs') or [])
            if proof_key in consumed:
                continue
            remaining = [dep for dep in runtime.get('blocked_resources') or []
                if dep.get('resource') != resource or dep.get('condition_revision') != episode['condition_revision']]
            runtime.update(recovery_proofs=[*consumed, proof_key], blocked_resources=remaining,
                           browser_method_repair=marker)
            if remaining:
                _save(c, row, row['state'], runtime)
                continue
            runtime.update(wait=None, lease_until=None, attempt_token=None)
            _save(c, row, 'planned', runtime, when=utcnow())
            ref = {'agent_id': str(aid), 'task_id': str(row['id']),
                   'task_generation': row['input']['task_generation']}
            emit(c, aid, 'growth.work', ref)
            refs.append(ref)
        return refs


def _episode(c, resource, condition, row, code=None):
    return one(c, """SELECT id,result#>'{runtime,recovery}' recovery FROM lead.tasks
        WHERE result#>>'{runtime,recovery,resource}'=%s
          AND result#>>'{runtime,recovery,condition_revision}'=%s
          AND (result#>>'{runtime,recovery,error_code}'<>'invalid_proposal'
            OR result#>>'{runtime,recovery,proposal_key}'=%s)
        ORDER BY (result#>>'{runtime,recovery,status}'='blocked') DESC,created_at,id LIMIT 1""",
        (resource, condition, _proposal_key(row)))


def _blocked_notice(c, aid, root, episode):
    if episode.get("blocked_notified"):
        return
    episode["blocked_notified"] = True
    c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s",
              (j(episode), root))
    emit(c, str(aid), "growth.wake", {"reason": "resource_blocked", "resource": episode["resource"],
         "condition_revision": episode["condition_revision"], "transition": digest([episode["fingerprint"], episode.get("transition", 0)])})


def _block_on(c, aid, row, runtime, root, episode, now):
    blocked = list(runtime.get("blocked_resources") or [])
    dependency = {"episode_root": str(root), "resource": episode["resource"],
                  "condition_revision": episode["condition_revision"]}
    if dependency not in blocked:
        blocked.append(dependency)
    task_deadline = TaskSpec.model_validate(row["input"]["spec"]).review_at
    check_at = min(datetime.fromisoformat(episode["next_check_at"]), task_deadline)
    if episode.get("probe_deadline") and episode.get("hard_deadline"):
        episode["probe_deadline"] = min(max(datetime.fromisoformat(episode["probe_deadline"]), task_deadline),
                                        datetime.fromisoformat(episode["hard_deadline"])).isoformat()
    wait = _wait(row, "resource_blocked", now, producer="growth.runtime.probe_episode", action="probe",
                 delay=max(1, int((check_at-now).total_seconds())))
    wait.update(episode_root=str(root), scope_ref=episode["resource"])
    runtime.update(blocked_resources=blocked, wait=wait)
    _blocked_notice(c, aid, root, episode)
    c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s", (j(episode),root))
    return wait


def check_research_resource(aid, task_id, scope):
    """Call at the actual tool boundary; alternatives remain free to run."""
    with tx(aid) as c:
        _recovery_lock(c, aid)
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE", (task_id,))
        if not row or row["state"] in TERMINAL:
            return None
        resource, condition = _current_research_resource(c, scope)
        episode = _episode(c, resource, condition, row)
        if not episode or episode["recovery"]["status"] != "blocked":
            return None
        runtime = _runtime(row)
        wait = _block_on(c, aid, row, runtime, episode["id"], dict(episode["recovery"]), utcnow())
        # The active invocation finishes its original claim with this Wait. Do not
        # revoke that claim here: it may still obtain useful alternative evidence.
        _save(c, row, row["state"], runtime)
        return Wait.model_validate(wait)


def _record_failure(c, row, runtime, code):
    resource, condition = runtime.get("resource_key", row["type"]), runtime.get("condition_revision", "initial")
    prior = _episode(c, resource, condition, row, code)
    root = prior["id"] if prior else row["id"]
    episode = dict(prior["recovery"]) if prior else {
        "fingerprint": digest([resource, condition]), "resource": resource,
        "condition_revision": condition, "root_task_id": str(root), "attempts": 0,
        "repair_attempts": 0, "probes": 0, "proposal_key": _proposal_key(row),
        "hard_deadline": (utcnow()+timedelta(hours=24)).isoformat(),
        "probe_deadline": min(TaskSpec.model_validate(row["input"]["spec"]).review_at,
                              utcnow()+timedelta(hours=24)).isoformat()}
    was_recovered = episode.get("status") == "recovered"
    episode["attempts"] += 1
    if was_recovered:
        episode.update(blocked_notified=False, transition=episode.get("transition", 0)+1)
    episode.update(last_failure_at=utcnow().isoformat(), error_code=code, status="blocked" if episode["attempts"] >= 3 or code in PERMANENT else "recovering",
                   next_check_at=(utcnow()+timedelta(minutes=min(60, 5*2**min(episode["probes"], 5)))).isoformat())
    if runtime.get("failure_scope"):
        episode["failure_scope"] = runtime["failure_scope"]
    runtime["episode_root"] = str(root)
    if episode["status"] == "blocked":
        _blocked_notice(c, row["agent_id"], root, episode)
    if root == row["id"]:
        runtime["recovery"] = episode
    else:
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s", (j(episode), root))
    return episode


def advance(value, attempt_token=None, *, now=None):
    ref = _ref(value)
    now = now or utcnow()
    with tx(str(ref.agent_id)) as c:
        _recovery_lock(c, str(ref.agent_id))
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
                return {"state":"terminal","reason":"product_changed"}
        resource, condition = _resource_condition(c, agent, row, spec)
        runtime.update(resource_key=resource, condition_revision=condition)
        episode = _episode(c, resource, condition,row)
        # Dynamic research chooses capabilities while executing. Only explicit
        # dependencies already recorded by an actual attempted call block it here.
        for dependency in runtime.get("blocked_resources") or []:
            pending = one(c, "SELECT result#>'{runtime,recovery}' recovery FROM lead.tasks WHERE id=%s",
                          (dependency["episode_root"],))
            current = (pending or {}).get("recovery") or {}
            if (current.get("status") == "blocked" and current.get("resource") == dependency["resource"]
                    and current.get("condition_revision") == dependency["condition_revision"] and not reconcile):
                wait = _block_on(c, str(ref.agent_id), row, runtime, dependency["episode_root"], current, now)
                _save(c, row, "planned", runtime, when=wait["check_at"])
                return {"state": "wait", "wait": wait}
        if episode and episode["recovery"]["status"] == "blocked" and not reconcile and spec.kind != "research":
            wait = _block_on(c, str(ref.agent_id), row, runtime, episode["id"], dict(episode["recovery"]), now)
            _save(c, row, "planned", runtime, when=wait["check_at"])
            return {"state": "wait", "wait": wait}
        for dependency in spec.required_refs:
            before = one(c, "SELECT state,result FROM lead.tasks WHERE id=%s", (dependency.task_id,))
            if not before or before["state"] in ("failed", "canceled"):
                _save(c, row, "failed", {**runtime, "reason": "dependency_failed"})
                return {"state": "terminal", "reason": "dependency_failed"}
            if before["state"] != "succeeded":
                wait = _wait(row, "dependency_pending", now)
                _save(c, row, "planned", {**runtime, "wait": wait}, when=wait["check_at"])
                return {"state": "wait", "wait": wait}
            produced = (before["result"] or {}).get("output") or {}
            if dependency.artifact!="output" and not any(
                item.get("kind")==dependency.artifact for item in produced.get("artifacts",[])+produced.get("evidence_refs",[])):
                _save(c,row,"failed",{**runtime,"reason":"dependency_artifact_missing"})
                return {"state":"terminal","reason":"dependency_artifact_missing"}
        if spec.account_id or spec.payload.get("profile_id"):
            account = one(c, """SELECT s.*,i.desired FROM lead.social_accounts s
                LEFT JOIN lead.account_operating_intents i ON i.account_id=s.id WHERE s.id=%s""", (spec.account_id,))
            if not reconcile and spec.account_id and (not account or account["binding_version"] != row["input"].get("binding_version")):
                _save(c, row, "canceled", {**runtime, "reason": "account_changed"})
                return {"state": "terminal", "reason": "account_changed"}
            if not reconcile and account and account["desired"] != "running":
                wait = _wait(row, "account_paused", now, producer="account_intents", action="configuration", owner="owner")
                _save(c, row, "planned", {**runtime, "wait": wait}, when=wait["check_at"])
                return {"state": "wait", "wait": wait}
            profile, native, scoped_account = _browser_resource(c, row, spec)
            group = 'profile:' + str(profile)
            resource = 'account:' + str(scoped_account) if native and scoped_account else group
            # Short admission serialization; never hold this lock across browser IO.
            if not one(c, "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) ok", (group,))["ok"]:
                return {"state": "wait", "wait": _wait(row, "resource_wait", now, delay=1, owner="resource")}
            running = all_rows(c, """SELECT t.id,t.result#>>'{runtime,resource}' resource,
                    coalesce(t.input#>>'{spec,account_id}',t.input#>>'{spec,payload,connection_account_id}') account_id,
                    (t.type='growth.account' AND t.input#>>'{spec,purpose}'='connect') authenticating,
                    EXISTS(SELECT FROM lead.actions a WHERE a.task_id=t.id AND a.state IN ('dispatching','unknown','confirmed')) readonly
                FROM lead.tasks t WHERE t.id<>%s AND t.state='running'
                AND coalesce(t.result#>>'{runtime,browser_profile_id}',t.input->>'profile_id',t.input#>>'{spec,payload,profile_id}')=%s
                AND (t.result#>>'{runtime,lease_until}')::timestamptz>%s""", (row['id'],str(profile),now))
            busy = bool(running)
            if native and scoped_account:
                # Only the current canonical Action state grants a read slot;
                # neither a runtime flag nor a caller's claim changes write
                # exclusivity. Reads still count toward the two native slots.
                readonly=bool(original and original['state'] in ('dispatching','unknown','confirmed'))
                authenticating=spec.kind=='account' and spec.purpose=='connect'
                busy = len(running) >= 2 or any(item['resource'] == group or
                    item['account_id'] == str(scoped_account) and (authenticating or item['authenticating']
                        or not readonly and not item['readonly']) for item in running)
                control = one(c, """SELECT id FROM lead.takeover_sessions t WHERE profile_id=%s
                    AND state IN ('requested','active','ending') AND expires_at>%s
                    AND (%s::uuid IS NULL OR id<>%s)
                    AND (purpose<>'login' OR connection_account_id=%s OR EXISTS(SELECT FROM lead.tasks w
                        WHERE w.input#>>'{spec,payload,takeover_id}'=t.id::text
                        AND w.input#>>'{spec,payload,connection_account_id}'=%s)) LIMIT 1""",
                    (profile,now,spec.payload.get('takeover_id'),spec.payload.get('takeover_id'),scoped_account,str(scoped_account)))
                busy = busy or bool(control)
            if busy:
                wait = _wait(row, "resource_wait", now, delay=1, owner="resource")
                _save(c, row, "queued", {**runtime, "wait": wait}, when=wait["check_at"])
                return {"state": "wait", "wait": wait}
            runtime["resource"] = resource
            runtime['browser_profile_id'] = str(profile)
        attempts = runtime.get("attempts", 0)
        if runtime.get("failure_attempts", 0) >= 3 and not runtime.get("recovery_permit") and not reconcile:
            _save(c, row, "failed", {**runtime, "reason": "recovery_exhausted"})
            return {"state": "terminal", "reason": "recovery_exhausted"}
        token = str(uuid4())
        if runtime.get("recovery_permit"):
            runtime["recovery_permit"] -= 1
        runtime.update(attempt_token=token, attempts=attempts + 1, wait=None,
                       control_version=agent["control_version"],
                       lease_until=(now + timedelta(minutes=9)).isoformat(),
                       last_progress_at=now.isoformat(), operation_ref=str(original["id"]) if original else None,
                       reconcile_only_action_id=str(original['id']) if original and original['state'] in ('dispatching','unknown') else None)
        _save(c, row, "running", runtime)
        c.execute("UPDATE lead.tasks SET started_at=coalesce(started_at,now()) WHERE id=%s", (row["id"],))
        claim = {"ref": ref.model_dump(mode="json"), "attempt_token": token,
                 "lease_until": runtime["lease_until"], "operation_ref": runtime["operation_ref"], "kind": spec.kind,
                 "control_version": runtime["control_version"]}
        return {"state": "ready", "claim": claim}


def finish(claim, value):
    accepted = _finish(claim, value)
    result = WorkResult.model_validate(value)
    if accepted["accepted_control"] and result.outcome == "completed" and claim.get("kind") in ("research", "observe"):
        try:
            from ..experiences import extract_task
            ref = _ref(claim["ref"])
            extract_task(str(ref.agent_id), str(ref.task_id))
        except Exception:
            # The original result is already committed. Optional memory cannot
            # roll it back, hold the task lease, or cause a new paid execution.
            import logging
            logging.getLogger(__name__).info("Optional experience extraction unavailable")
    return accepted


def _finish(claim, value):
    result = WorkResult.model_validate(value)
    ref = _ref(claim["ref"])
    with tx(str(ref.agent_id)) as c:
        _recovery_lock(c, str(ref.agent_id))
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
        # Admission records the original action's state before execution. The
        # initial send/prepared window isn't a completed receipt check, and a
        # result/claim payload cannot invent this server-side scope or counter.
        reconcile_action = runtime.pop('reconcile_only_action_id', None)
        if action and reconcile_action == str(action['id']):
            progress = runtime.get('receipt_reconciliation') or {}
            completed = progress.get('completed_checks', 0) if progress.get('action_id') == reconcile_action else 0
            runtime['receipt_reconciliation'] = {'action_id': reconcile_action, 'completed_checks': completed + 1}
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
                runtime["failure_scope"] = result.output.get("failure_scope")
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
            delay = _receipt_delay(runtime, action)
            wait = _wait(row, "unknown_receipt", utcnow(), action="reconcile", delay=delay,
                         owner='platform' if action else 'product')
            if action:
                wait['scope_ref'] = str(action['id'])
            if delay == 3600:
                wait['escalation'] = '原动作结果仍待核对；平台每小时回查，不再次提交相同意图。'
            _save(c, row, "reconciling", {**runtime, "wait": wait}, output, wait["check_at"])
        elif result.outcome == "waiting":
            wait = result.wait.model_dump(mode="json")
            if action and action['state'] in ('dispatching','unknown') and _receipt_delay(runtime, action) == 3600:
                wait['check_at'] = max(datetime.fromisoformat(wait['check_at']), utcnow() + timedelta(hours=1)).isoformat()
            state = "waiting_user" if result.wait.recovery_action == "owner" else "planned"
            connection = None
            if (row['type'] == 'growth.account' and result.wait.producer == 'account_connection'
                    and result.wait.recovery_action == 'owner'):
                # Owner resume can commit while this old worker still holds its
                # claim. Its late owner Wait must not swallow the real wake.
                connection = one(c, "SELECT * FROM lead.takeover_sessions WHERE id=%s AND purpose='login' FOR UPDATE",
                    (row['input']['spec']['payload'].get('takeover_id'),))
            now = utcnow()
            if (connection and connection['state'] == 'active' and connection['control_mode'] == 'automatic'
                    and connection['control_version'] == claim['control_version'] and connection['expires_at'] > now):
                _save(c, row, 'planned', {**runtime, 'wait': None}, output, now)
                emit(c, str(ref.agent_id), 'growth.work', ref.model_dump(mode='json'))
            else:
                _save(c, row, state, {**runtime, "wait": wait}, output, wait["check_at"])
        elif result.failure_code in PERMANENT or runtime.get("failure_attempts", 0) >= 3:
            _save(c, row, "failed", {**runtime, "reason": result.failure_code}, output)
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
            from .. import local_executor
            from ..local_executor_client import execution_context
            native = local_executor.enabled(str(ref.agent_id))
            context_token = execution_context.set({"key": "task:" + str(ref.task_id) + ":" + str(ref.task_generation),
                "task_id": str(ref.task_id), "session_key": "decision:" + str(ref.agent_id) + ":" + str(row["input"].get("goal_revision", 1))
                if claim.get("kind") == "decision" else "task:" + str(ref.task_id)})
            try:
                operation = execute_tool(str(ref.agent_id), row, claim)
                result = await operation if native else await asyncio.wait_for(operation, timeout=480)
            finally:
                execution_context.reset(context_token)
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
        _recovery_lock(c, aid)
        # A connection's task may expire before it executes again. Persist the
        # expiry independently so encrypted input and the original page role do
        # not survive their deadline merely because the task is now terminal.
        expired_connections = all_rows(c, """UPDATE lead.takeover_sessions
            SET state='expired',ended_at=coalesce(ended_at,%s),encrypted_login=NULL,
                challenge_version=challenge_version+1,input_seq=input_seq+1
            WHERE purpose='login' AND state IN ('requested','active','ending') AND expires_at<=%s
            RETURNING id,profile_id""", (now,now))
        for connection in expired_connections:
            emit(c,aid,"growth.wake",{"reason":"connection_expired",
                "connection_id":str(connection["id"]),"profile_id":str(connection["profile_id"])})
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
            if episode.get("probes_stopped"):
                continue
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


def _resume_allowed(c, agent, row):
    spec = TaskSpec.model_validate(row["input"]["spec"])
    if not enabled(agent) or agent["desired_state"] in ("paused", "archived"):
        return "project_paused"
    if spec.review_at <= utcnow():
        return "work_window_ended"
    if one(c, "SELECT id FROM lead.actions WHERE task_id=%s AND state IN ('dispatching','unknown','confirmed')", (row["id"],)):
        return "reconcile_original_action"
    product_research = spec.kind == "research" and spec.purpose == "product" and not spec.experiment_id
    if not product_research and row["input"].get("goal_revision") != objective_revision(agent):
        return "objective_changed"
    business = one(c, "SELECT id FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
    if "business_profile_id" in row["input"] and row["input"]["business_profile_id"] != (str(business["id"]) if business else None):
        return "product_changed"
    if spec.experiment_id:
        experiment = one(c, "SELECT state,specification FROM lead.growth_experiments WHERE id=%s", (spec.experiment_id,))
        if (not experiment or experiment["state"] not in ("active", "observing")
                or datetime.fromisoformat(experiment["specification"]["review_at"]) <= utcnow()):
            return "experiment_closed"
    if spec.account_id:
        account = one(c, """SELECT s.binding_version,s.state,i.desired,i.scope FROM lead.social_accounts s
            LEFT JOIN lead.account_operating_intents i ON i.account_id=s.id WHERE s.id=%s""", (spec.account_id,))
        if not account or account["binding_version"] != row["input"].get("binding_version"):
            return "account_changed"
        if account["state"] != "connected" or account["desired"] != "running":
            return "account_paused"
    if spec.target_id:
        target = one(c, """SELECT t.external_target_id FROM lead.account_targets t
            JOIN lead.authorization_grants g ON g.target_id=t.id AND g.revoked_at IS NULL
            WHERE t.id=%s AND t.account_id=%s AND t.state='active'""", (spec.target_id, spec.account_id))
        if not target:
            return "target_not_permitted"
        scope = account.get("scope") or {}
        if target["external_target_id"] in scope.get("excluded_targets", []):
            return "target_not_permitted"
        if scope.get("target_scope") == "existing_grants" and target["external_target_id"] not in scope.get("target_ids", []):
            return "target_not_permitted"
    return None


def _resume_proven(c, aid, agent, row, episode, proof_ref):
    if row["state"] not in ("failed", "planned", "queued", "waiting_user"):
        return None
    runtime = _runtime(row)
    consumed = list(runtime.get("recovery_proofs") or [])
    proof_key = digest([episode["resource"], episode["condition_revision"], proof_ref])
    if proof_key in consumed or _resume_allowed(c, agent, row):
        return None
    dependencies = list(runtime.get("blocked_resources") or [])
    def matches(dep):
        return dep.get("resource") == episode["resource"] and dep.get("condition_revision") == episode["condition_revision"]
    if not any(matches(dep) for dep in dependencies) and not (
            runtime.get("resource_key") == episode["resource"] and runtime.get("condition_revision") == episode["condition_revision"]):
        return None
    remaining = [dep for dep in dependencies if not matches(dep)]
    runtime.update(blocked_resources=remaining, recovery_proofs=[*consumed, proof_key])
    if remaining:
        _save(c, row, row["state"], runtime)
        return None
    if (runtime.get("recovery") or {}).get("resource") == episode["resource"]:
        runtime["recovery"] = {**episode, "resumed": True}
    runtime.update(wait=None, lease_until=None, attempt_token=None, reason="condition_recovered", recovery_permit=1)
    new_input = {**row["input"], "task_generation": row["input"]["task_generation"]+1}
    if runtime.get('browser_method_repair'):
        runtime['browser_method_repair'] = {**runtime['browser_method_repair'],
            'task_generation': new_input['task_generation']}
    c.execute("UPDATE lead.tasks SET input=%s WHERE id=%s", (j(new_input), row["id"]))
    _save(c, row, "planned", runtime, when=utcnow())
    ref = {"agent_id": str(aid), "task_id": str(row["id"]), "task_generation": new_input["task_generation"]}
    emit(c, aid, "growth.work", ref)
    return ref


def resume_task(aid, task_id, *, proof_ref=None):
    """A configuration change permits a new check, never proves an outage resolved."""
    with tx(aid) as c:
        _recovery_lock(c, aid)
        agent = require_agent(c, aid, True)
        # Match the existing control order: project, account, then task. These
        # short shared locks keep a current permission read ordered with revoke.
        all_rows(c, "SELECT id FROM lead.social_accounts ORDER BY id FOR SHARE")
        all_rows(c, "SELECT id FROM lead.authorization_grants ORDER BY id FOR SHARE")
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE", (task_id,))
        if not row or row["state"] != "failed":
            return None
        runtime = _runtime(row)
        episode = runtime.get("recovery") or {}
        if not (proof_ref and episode.get("proof_ref") == proof_ref and episode.get("status") == "recovered"):
            raise ValueError("recovery_condition_unchanged")
        if _resume_allowed(c, agent, row) == "reconcile_original_action":
            raise ValueError("reconcile_original_action")
        return _resume_proven(c, aid, agent, row, episode, proof_ref)


def _consume_recovery(aid, root_id, expected, proof, observed_condition, lease_token):
    with tx(aid) as c:
        _recovery_lock(c, aid)
        agent = require_agent(c, aid, True)
        all_rows(c, "SELECT id FROM lead.social_accounts ORDER BY id FOR SHARE")
        all_rows(c, "SELECT id FROM lead.authorization_grants ORDER BY id FOR SHARE")
        root = one(c, "SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE", (root_id,))
        if not root:
            return None
        episode = dict(_runtime(root).get("recovery") or {})
        _, current = _resource_condition(c, agent, root, TaskSpec.model_validate(root["input"]["spec"]))
        if (episode.get("status") != "blocked" or episode.get("resource") != expected["resource"]
                or episode.get("condition_revision") != expected["condition_revision"]
                or episode.get("probe_token") != lease_token or current != observed_condition
                or episode.get("attempts") != expected.get("attempts")):
            return None
        episode.update(status="recovered", proof_ref=proof, observed_condition=observed_condition, probe_lease_until=None)
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s", (j(episode), root_id))
        rows = all_rows(c, """SELECT * FROM lead.tasks WHERE type LIKE 'growth.%%'
            AND state IN ('failed','planned','queued','waiting_user')
            AND ((result#>>'{runtime,resource_key}'=%s AND result#>>'{runtime,condition_revision}'=%s)
              OR result#>'{runtime,blocked_resources}' @> %s)
            ORDER BY id FOR UPDATE""", (episode["resource"], episode["condition_revision"],
            j([{"resource":episode["resource"], "condition_revision":episode["condition_revision"]}])))
        resumed = [ref for row in rows if (ref := _resume_proven(c, aid, agent, row, episode, proof))]
        emit(c, aid, "growth.wake", {"reason":"resource_recovered", "resource":episode["resource"],
             "condition_revision":episode["condition_revision"], "transition":episode["fingerprint"],
             "proof_ref":proof, "resumed_count":len(resumed)})
        return resumed


async def probe_episode(aid, work, data):
    with tx(aid) as c:
        _recovery_lock(c, aid)
        agent = require_agent(c,aid)
        root = one(c,"SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE",(data.episode_root,))
        if not root:
            raise ValueError("recovery_scope")
        episode = dict(_runtime(root).get("recovery") or {})
        if episode.get("status") != "blocked" or episode.get("condition_revision") != data.condition_revision:
            return WorkResult(outcome="completed",output={"state":"obsolete_probe"})
        deadline = datetime.fromisoformat(episode.get("probe_deadline") or root["input"]["spec"]["review_at"])
        if episode.get("probes_stopped") or episode.get("probes", 0) >= 8 or utcnow() >= deadline:
            if not episode.get("probes_stopped"):
                episode.update(probes_stopped=True, reason="recovery_probe_limit" if episode.get("probes",0)>=8 else "recovery_deadline",
                               responsibility="product", recovery_action="configuration")
                c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s", (j(episode),root["id"]))
                emit(c,aid,"growth.wake",{"reason":"resource_probe_stopped", "resource":episode["resource"],
                     "condition_revision":episode["condition_revision"], "transition":episode["fingerprint"]})
            return WorkResult(outcome="completed", output={"state":"recovery_budget_exhausted", "reason":episode["reason"],
                "resource":episode["resource"], "responsibility":"product", "resumed_count":0})
        if episode.get("probe_lease_until") and datetime.fromisoformat(episode["probe_lease_until"])>utcnow():
            return WorkResult(outcome="completed",output={"state":"another_probe_active"})
        spec = TaskSpec.model_validate(root["input"]["spec"])
        _, actual_condition = _resource_condition(c,agent,root,spec)
        changed = actual_condition != episode["condition_revision"]
        if episode.get("paid_probes_stopped") and not changed:
            return WorkResult(outcome="completed",output={"state":"recovery_budget_exhausted","responsibility":"product"})
        lease_token = str(uuid4())
        episode.update(probe_token=lease_token, probes=episode.get("probes",0)+1,probe_lease_until=(utcnow()+timedelta(minutes=9)).isoformat())
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s) WHERE id=%s",(j(episode),root["id"]))
    proof = None
    try:
        if spec.kind == "research" and (episode.get("failure_scope") or episode.get("error_code") in ("source_unavailable","gaierror")):
            from ..budget import growth_cost_context
            from .tools import research
            token = growth_cost_context.set({"task_id":str(root["id"]),
                "experiment_id":str(root["experiment_id"]) if root["experiment_id"] else None,
                "recovery_root":str(root["id"]),"recovery_limit":70000})
            try:
                if episode.get("failure_scope"):
                    from .tools import probe_research_resource
                    observation = await probe_research_resource(aid, root, {
                        **episode["failure_scope"], "resource": episode["resource"],
                        "condition_revision": episode["condition_revision"], "observed_condition": actual_condition}, work)
                else:
                    observation = await research(aid,root,spec)
            finally:
                growth_cost_context.reset(token)
            if episode.get("failure_scope"):
                checks = observation.output.get("recovery_checks") or []
                match = next((check for check in checks if check.get("resource") == episode["resource"]
                    and check.get("condition_revision") == episode["condition_revision"]
                    and check.get("state") == "ready" and check.get("evidence_ref")), None)
                if observation.outcome == "completed" and match:
                    proof = match["evidence_ref"]
            elif observation.outcome == "completed" and observation.evidence_refs:
                # Legacy episodes lack actual-call scope; evidence must still be
                # from an original URL, never an unrelated successful fallback.
                with tx(aid) as c:
                    urls = spec.payload.get("urls") or []
                    if not urls:
                        business = one(c, "SELECT product_url FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
                        urls = [business["product_url"]] if business and business["product_url"] else []
                    for evidence in observation.evidence_refs:
                        document = one(c, "SELECT source_ref FROM lead.knowledge_documents WHERE id=%s AND deleted_at IS NULL", (evidence.id,))
                        if document and (not urls or document["source_ref"] in urls):
                            proof = "document:"+str(evidence.id)
                            break
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
            resumed = await asyncio.to_thread(_consume_recovery, aid, root["id"], episode, proof, actual_condition, lease_token)
            if resumed is None:
                return WorkResult(outcome="completed", output={"state":"obsolete_probe"})
            return WorkResult(outcome="completed",output={"state":"recovered", "resumed":resumed[0] if resumed else None,
                "resumed_refs":resumed, "resumed_count":len(resumed), "resource":episode["resource"],
                "condition_revision":episode["condition_revision"], "proof_ref":proof})
        return WorkResult(outcome="completed",output={"state":"still_blocked","responsibility":"product",
            "resource":episode["resource"], "reason":episode.get("error_code"),
            "condition_revision":episode["condition_revision"],"next_check_at":episode["next_check_at"]})
    except Exception as error:
        from ..budget import BudgetExceeded
        if not isinstance(error,BudgetExceeded) or str(error)!="recovery_budget_exhausted":
            raise
        episode.update(paid_probes_stopped=True,reason="recovery_budget_exhausted",
                       responsibility="product",recovery_action="configuration")
        with tx(aid) as c:
            c.execute("""UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',%s)
                WHERE id=%s AND result#>>'{runtime,recovery,probe_token}'=%s""", (j(episode),root["id"],lease_token))
        return WorkResult(outcome="completed",output={"state":"recovery_budget_exhausted","responsibility":"product"})
    finally:
        with tx(aid) as c:
            c.execute("""UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery,probe_lease_until}','null')
                WHERE id=%s AND result#>>'{runtime,recovery,probe_token}'=%s""", (root["id"], lease_token))


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
    if item.get("reason") in ("resource_blocked", "resource_recovered", "resource_probe_stopped"):
        return digest([item.get("reason"), item.get("resource"), item.get("condition_revision"), item.get("transition")])
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
    from .. import local_executor
    from ..local_executor_client import activity_jobs
    if local_executor.enabled(args["agent_id"]):
        claim = args["claim"]
        key = "growth:" + str(claim["ref"]["task_id"]) + ":" + claim["attempt_token"]
        with tx(args["agent_id"]) as c:
            row = one(c, "SELECT t.*,a.control_version current_control FROM lead.tasks t JOIN lead.agents a ON a.id=t.agent_id WHERE t.id=%s FOR UPDATE OF t", (claim["ref"]["task_id"],))
            runtime = _runtime(row or {})
            if (not row or row["state"] != "running" or runtime.get("attempt_token") != claim["attempt_token"]
                    or row["current_control"] != claim.get("control_version")):
                activity_jobs.cancel(key)
                return WorkResult(outcome="failed", failure_code="scope_changed").model_dump(mode="json")
            runtime["lease_until"] = (utcnow() + timedelta(minutes=9)).isoformat()
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime}',%s) WHERE id=%s", (j(runtime), row["id"]))
        return await activity_jobs.step(key, lambda: execute(claim))
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
