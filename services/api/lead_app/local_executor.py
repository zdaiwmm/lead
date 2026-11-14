"""Durable transport for a user's official Codex runtime.

The transport owns neither business completion nor external-send identity. In
particular, an uncertain app-server start is never silently issued again.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import shlex
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from .config import CONFIG
from .db import all_rows, audit, emit, j, one, require_agent, tx
from .scope import current_tenant, tenant_scope

MODEL = "gpt-6.1-sol"
EFFORT = "medium"
TERMINAL = {"completed", "failed", "interrupted", "execution_unknown", "canceled"}
ONLINE_SECONDS = 90
MANAGED_APPROVAL_METHODS = frozenset({'item/commandExecution/requestApproval',
    'item/fileChange/requestApproval', 'item/permissions/requestApproval'})

_SOURCE_FILTER = """coalesce(r.scope#>>'{execution_route,source}','personal_v1')=%s
    AND (%s::text IS NULL OR (r.scope#>>'{execution_route,grant_id}'=%s
        AND coalesce(r.scope#>>'{execution_route,grant_generation}','1')=%s))"""


def _now():
    return datetime.now(timezone.utc)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def execution_source(scope):
    """Missing historical metadata always means personal, even with no device."""
    if not isinstance(scope, dict):
        raise ValueError('executor_request_conflict')
    route = scope.get('execution_route')
    if route is None:
        return 'personal_v1'
    if not isinstance(route, dict) or route.get('source') not in {'personal_v1', 'managed_node'}:
        raise ValueError('executor_request_conflict')
    if route['source'] == 'managed_node':
        try:
            UUID(str(route['grant_id']))
            if not re.fullmatch(r'execution/default@[0-9]+', route['policy_revision']):
                raise ValueError
            if ('grant_generation' in route
                    and (type(route['grant_generation']) is not int or route['grant_generation'] < 1)):
                raise ValueError
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError('executor_request_conflict') from error
    return route['source']


def _source_parameters(source, grant_id=None, grant_generation=None):
    if source == 'personal_v1' and grant_id is None and grant_generation is None:
        return source, None, None, None
    if source == 'managed_node' and type(grant_generation) is int and grant_generation > 0:
        try:
            grant = str(UUID(str(grant_id)))
            return source, grant, grant, str(grant_generation)
        except (ValueError, TypeError):
            pass
    raise ValueError('executor_scope_forbidden')


def _source_matches(request, source, grant_id=None, grant_generation=None):
    _source_parameters(source, grant_id, grant_generation)
    if execution_source(request['scope']) != source:
        return False
    route = request['scope'].get('execution_route') or {}
    return source == 'personal_v1' or (str(route['grant_id']) == str(grant_id)
        and route.get('grant_generation', 1) == grant_generation)


def _require_request_source(request, source, grant_id=None, grant_generation=None):
    if not _source_matches(request, source, grant_id, grant_generation):
        raise ValueError('executor_request_unavailable')


def _project_source(c, project, *, policy=None):
    from . import executor_nodes

    override = (project or {}).get('execution_override', 'inherit')
    if override == 'legacy':
        return 'legacy'
    if override == 'local_device':
        return 'personal_v1' if project.get('mode') == 'codex' else 'legacy'
    if override != 'inherit':
        raise ValueError('executor_scope_changed')
    selected = policy if policy is not None else executor_nodes.policy(c)
    if selected.get('source') not in {'legacy', 'managed_node'}:
        raise ValueError('executor_not_ready')
    if selected['source'] == 'managed_node':
        return 'managed_node'
    return 'personal_v1' if project and project['mode'] == 'codex' else 'legacy'


def _token(kind, **claims):
    raw = base64.urlsafe_b64encode(_encoded({"kind": kind, **claims}).encode()).decode().rstrip("=")
    sig = hmac.new(CONFIG["session_secret"].encode(), ("local-executor-v1:"+raw).encode(), hashlib.sha256).hexdigest()
    return raw+"."+sig


def _claims(value, kind):
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError
        raw, sig = value.split(".")
        expected = hmac.new(CONFIG["session_secret"].encode(), ("local-executor-v1:"+raw).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(raw+"="*(-len(raw) % 4)))
        if claims["kind"] != kind or claims["expires"] <= time.time():
            raise ValueError
        for field in ("tenant_id", "agent_id", "device_id"):
            claims[field] = str(UUID(claims[field]))
        return claims
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("pairing_unavailable" if kind == "pair" else "executor_device_unavailable") from error


def _online(device):
    return bool(device and device["state"] == "active" and device.get("last_seen_at")
                and (_now()-device["last_seen_at"]).total_seconds() <= ONLINE_SECONDS
                and device.get("capabilities", {}).get("ready"))


def default_agent(*, connection=None):
    """Only the current tenant's explicitly paired, nonarchived projects qualify."""
    with (nullcontext(connection) if connection is not None else tx()) as c:
        row = one(c, """SELECT p.agent_id FROM lead.local_executor_projects p
            JOIN lead.agents a ON a.id=p.agent_id AND a.tenant_id=p.tenant_id
            JOIN lead.local_executor_devices d ON d.id=p.device_id AND d.tenant_id=p.tenant_id AND d.agent_id=p.agent_id
            WHERE p.mode='codex' AND d.state='active' AND a.desired_state<>'archived'
            ORDER BY p.updated_at DESC,p.agent_id LIMIT 1""")
        return str(row["agent_id"]) if row else None


def enabled(aid=None, *, connection=None):
    from . import executor_nodes

    with (nullcontext(connection) if connection is not None else tx(str(aid) if aid else None)) as c:
        if aid is None:
            policy = executor_nodes.policy(c)
            if policy.get('source') not in {'legacy', 'managed_node'}:
                raise ValueError('executor_not_ready')
            return policy['source'] == 'managed_node'
        row = one(c, "SELECT * FROM lead.local_executor_projects WHERE agent_id=%s", (aid,))
        return _project_source(c, row) != 'legacy'


def configured(aid=None):
    from . import executor_nodes

    with tx(str(aid) if aid else None) as c:
        project = one(c, "SELECT * FROM lead.local_executor_projects WHERE agent_id=%s", (aid,)) if aid else None
        source = _project_source(c, project) if aid else (
            'managed_node' if executor_nodes.policy(c)['source'] == 'managed_node' else 'legacy')
        if source == 'managed_node':
            return bool(executor_nodes.server_status(c).get('ready'))
        if source != 'personal_v1' or not project.get('device_id'):
            return False
        if executor_nodes.is_managed_device(c, project['device_id']):
            return False
        return _online(one(c, "SELECT * FROM lead.local_executor_devices WHERE id=%s", (project['device_id'],)))


def bound(aid, *, task_id=None, key=None, connection=None):
    """An in-flight job keeps its original execution route across mode changes."""
    if not aid or not (task_id or key):
        return False
    with (nullcontext(connection) if connection is not None else tx(str(aid))) as c:
        if task_id:
            return bool(one(c, """SELECT r.id FROM lead.local_executor_requests r JOIN lead.tasks t ON t.id=r.task_id
                AND t.tenant_id=r.tenant_id AND t.agent_id=r.agent_id WHERE r.task_id=%s
                AND coalesce(t.input->>'task_generation','1')=coalesce(r.scope->>'task_generation','1') LIMIT 1""", (task_id,)))
        return bool(one(c, "SELECT id FROM lead.local_executor_requests WHERE left(request_key,%s)=%s LIMIT 1", (len(key)+1, key+":")))


def create_pairing(aid):
    from .auth import public_origin
    from .local_executor_identity import capture

    aid = str(aid)
    device_id = str(uuid4())
    expires = _now()+timedelta(minutes=5)
    with tx(aid) as c:
        require_agent(c, aid, True)
        code = _token("pair", tenant_id=current_tenant(), agent_id=aid, device_id=device_id,
                      owner=capture(c, aid), nonce=secrets.token_urlsafe(24), expires=int(expires.timestamp()))
        c.execute("""INSERT INTO lead.local_executor_devices(id,tenant_id,agent_id,pairing_hash,pairing_expires_at)
            VALUES(%s,%s,%s,%s,%s)""", (device_id, current_tenant(), aid, _digest(code), expires))
        audit(c, aid, "executor.pairing_created", {"device_id": device_id})
    return {"code": code, "expires_at": expires.isoformat(), "device_id": device_id,
            "connect_command": "lead-executor pair --profile "+aid+" --url "+shlex.quote(public_origin())+" --code "+code+" --install"}


def pair(code, name, platform, capabilities):
    from .local_executor_identity import require_current

    claims = _claims(code, "pair")
    with tenant_scope(claims["tenant_id"], None), tx(claims["agent_id"]) as c:
        require_agent(c, claims["agent_id"], True)
        require_current(c, claims.get("owner"), claims["agent_id"])
        device = one(c, "SELECT * FROM lead.local_executor_devices WHERE id=%s FOR UPDATE", (claims["device_id"],))
        if (not device or device["state"] != "pairing" or device["pairing_hash"] != _digest(code)
                or device["pairing_expires_at"] <= _now()):
            raise ValueError("pairing_unavailable")
        bearer = _token("device", tenant_id=claims["tenant_id"], agent_id=claims["agent_id"],
                        device_id=claims["device_id"], owner=claims["owner"], nonce=secrets.token_urlsafe(32),
                        expires=int(time.time())+30*86400)
        c.execute("""UPDATE lead.local_executor_devices SET state='active',name=%s,platform=%s,
            capabilities=%s,token_hash=%s,pairing_hash=NULL,pairing_expires_at=NULL,last_seen_at=now() WHERE id=%s""",
                  (name[:160], platform[:80], j(capabilities), _digest(bearer), device["id"]))
        c.execute("""INSERT INTO lead.local_executor_projects(tenant_id,agent_id,mode,device_id,execution_override)
            VALUES(%s,%s,'codex',%s,'local_device') ON CONFLICT(tenant_id,agent_id) DO UPDATE SET
            mode='codex',device_id=excluded.device_id,execution_override='local_device',updated_at=now()""",
                  (claims["tenant_id"], claims["agent_id"], device["id"]))
        audit(c, claims["agent_id"], "executor.paired", {"device_id": str(device["id"]), "platform": platform[:80]})
        _wake_existing(c, claims["agent_id"], "executor_paired")
        return {"device_id": str(device["id"]), "token": bearer, "epoch": device["epoch"],
                "agent_id": claims["agent_id"], "tenant_id": claims["tenant_id"], "model": MODEL, "effort": EFFORT}


def set_mode(aid, mode):
    from . import executor_nodes

    if mode not in {"legacy", "codex"}:
        raise ValueError("executor_mode_invalid")
    with tx(str(aid)) as c:
        require_agent(c, aid, True)
        project = one(c, 'SELECT * FROM lead.local_executor_projects WHERE agent_id=%s', (aid,))
        override = 'legacy' if mode == 'legacy' else 'local_device'
        if (mode == 'codex' and project and project.get('device_id')
                and executor_nodes.is_managed_device(c, project['device_id'])):
            override = 'inherit'
        c.execute("""INSERT INTO lead.local_executor_projects(tenant_id,agent_id,mode,execution_override)
            VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id,agent_id) DO UPDATE SET
            mode=excluded.mode,execution_override=excluded.execution_override,updated_at=now()""",
                  (current_tenant(), aid, mode, override))
        audit(c, str(aid), "executor.mode_changed", {"mode": mode, 'execution_override': override})
        _wake_existing(c, str(aid), "executor_mode_changed")
    return {"mode": mode, 'execution_override': override}


def set_inherit(aid):
    with tx(str(aid)) as c:
        require_agent(c, aid, True)
        c.execute("""INSERT INTO lead.local_executor_projects(tenant_id,agent_id,execution_override)
            VALUES(%s,%s,'inherit') ON CONFLICT(tenant_id,agent_id) DO UPDATE SET
            execution_override='inherit',updated_at=now()""", (current_tenant(), aid))
        audit(c, str(aid), 'executor.inheritance_restored', {})
        _wake_existing(c, str(aid), 'executor_inheritance_restored')
    return {'execution_override': 'inherit'}


def _wake_existing(c, aid, reason):
    agent = require_agent(c, aid)
    if agent["desired_state"] == "running" and one(c, "SELECT id FROM lead.business_profiles LIMIT 1"):
        emit(c, str(aid), "growth.wake", {"reason": reason})


def revoke(aid, device_id):
    from . import executor_nodes

    with tx(str(aid)) as c:
        require_agent(c, aid, True)
        if executor_nodes.is_managed_device(c, device_id):
            raise ValueError('executor_device_managed')
        row = one(c, """UPDATE lead.local_executor_devices SET state='revoked',token_hash=NULL,
            pairing_hash=NULL,revoked_at=now(),epoch=epoch+1 WHERE id=%s RETURNING id""", (device_id,))
        if not row:
            raise LookupError("executor_device_unavailable")
        audit(c, str(aid), "executor.revoked", {"device_id": str(device_id)})
    return {"revoked": True}


def _device(c, claims, bearer, epoch=None):
    from .local_executor_identity import require_current

    require_current(c, claims.get("owner"), claims["agent_id"])
    device = one(c, "SELECT * FROM lead.local_executor_devices WHERE id=%s FOR UPDATE", (claims["device_id"],))
    if (not device or device["state"] != "active" or device["token_hash"] != _digest(bearer)
            or str(device["agent_id"]) != claims["agent_id"]):
        raise ValueError("executor_device_unavailable")
    require_agent(c, claims["agent_id"])
    if epoch is not None and device["epoch"] != epoch:
        raise ValueError("executor_epoch_changed")
    return device


def _request(c, request_id, device=None, lock=False):
    row = one(c, "SELECT * FROM lead.local_executor_requests WHERE id=%s"+(" FOR UPDATE" if lock else ""), (request_id,))
    if not row or (device and any(str(row[key]) != str(device[other]) for key, other in
            (('device_id', 'id'), ('agent_id', 'agent_id'), ('tenant_id', 'tenant_id')))):
        raise ValueError("executor_request_unavailable")
    return row


def request_by_key(aid, request_key):
    with tx(str(aid)) as c:
        require_agent(c, aid)
        return one(c, "SELECT * FROM lead.local_executor_requests WHERE request_key=%s", (request_key,))


def submit(aid, request_key, payload, *, session_key, task_id=None, resume=False):
    from . import executor_nodes

    aid = str(aid or "")
    if not aid:
        raise ValueError("executor_not_configured")
    if not request_key or len(request_key) > 500 or not session_key or len(session_key) > 500:
        raise ValueError("executor_request_key_invalid")
    body = {**payload, "session_key": session_key, "model": MODEL, "effort": EFFORT}
    fingerprint = _digest(_encoded({"payload": body, "task_id": task_id}))
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        previous = one(c, "SELECT * FROM lead.local_executor_requests WHERE request_key=%s", (request_key,))
        if previous:
            if previous["request_hash"] != fingerprint:
                if (not resume or previous["session_key"] != session_key
                        or str(previous.get("task_id") or "") != str(task_id or "")
                        or previous["payload"].get("output_schema") != body.get("output_schema")):
                    raise ValueError("executor_request_conflict")
            return previous
        project = one(c, "SELECT * FROM lead.local_executor_projects WHERE agent_id=%s", (aid,))
        # A session is pinned to its first device. Re-pairing never migrates an
        # existing conversation or an uncertain send to a different runtime.
        session = one(c, """SELECT device_id,scope FROM lead.local_executor_requests WHERE session_key=%s
            ORDER BY created_at,id LIMIT 1""", (session_key,))
        task_binding = one(c, "SELECT device_id,scope FROM lead.local_executor_requests WHERE task_id=%s ORDER BY created_at,id LIMIT 1", (task_id,)) if task_id else None
        old = session or task_binding
        if old:
            source = execution_source(old['scope'])
            route = dict(old['scope'].get('execution_route') or {'source': 'personal_v1'})
            device_id = old['device_id']
            if source == 'managed_node' and device_id is None:
                raise ValueError('executor_request_conflict')
            if session and task_binding and (
                    str(session['device_id']) != str(task_binding['device_id'])
                    or execution_source(session['scope']) != execution_source(task_binding['scope'])
                    or (session['scope'].get('execution_route') or {}).get('grant_id') !=
                        (task_binding['scope'].get('execution_route') or {}).get('grant_id')
                    or (session['scope'].get('execution_route') or {}).get('grant_generation') !=
                        (task_binding['scope'].get('execution_route') or {}).get('grant_generation')):
                raise ValueError('executor_request_conflict')
        else:
            policy = executor_nodes.policy(c) if (project or {}).get('execution_override', 'inherit') == 'inherit' else None
            source = _project_source(c, project, policy=policy)
            if source == 'legacy':
                raise ValueError('executor_not_configured')
            if source == 'managed_node':
                binding = executor_nodes.project_binding(c, aid, policy)
                route, device_id = binding['execution_route'], binding['device_id']
                if device_id is None or execution_source({'execution_route': route}) != 'managed_node':
                    raise ValueError('executor_request_conflict')
                _source_parameters('managed_node', route['grant_id'], route['grant_generation'])
                route = {'source': 'managed_node', 'grant_id': str(UUID(str(route['grant_id']))),
                    'grant_generation': route['grant_generation'], 'policy_revision': route['policy_revision']}
            else:
                route, device_id = {'source': 'personal_v1'}, project.get('device_id')
                if device_id and executor_nodes.is_managed_device(c, device_id):
                    raise ValueError('executor_device_managed')
        scope = {"control_version": agent["control_version"], 'execution_route': route}
        if task_id:
            task = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (task_id,))
            if not task:
                raise ValueError("executor_task_unavailable")
            runtime = (task.get("result") or {}).get("runtime", {})
            scope.update(task_generation=task["input"].get("task_generation", 1),
                         attempt_token=runtime.get("attempt_token"))
        row = one(c, """INSERT INTO lead.local_executor_requests(tenant_id,agent_id,device_id,task_id,
            request_key,request_hash,session_key,payload,scope) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                  (current_tenant(), aid, device_id, task_id, request_key, fingerprint, session_key, j(body), j(scope)))
        _enqueue(c, row, "run", body, "run")
        return row


def request_status(aid, request_id):
    with tx(str(aid)) as c:
        require_agent(c, aid)
        row = one(c, "SELECT * FROM lead.local_executor_requests WHERE id=%s", (request_id,))
        if not row:
            raise LookupError("executor_request_unavailable")
        if row.get("result") and "output" in row["result"]:
            row["output"] = row["result"]["output"]
        return row


def status(aid):
    from . import executor_nodes

    with tx(str(aid)) as c:
        require_agent(c, aid)
        project = one(c, "SELECT * FROM lead.local_executor_projects WHERE agent_id=%s", (aid,))
        source = _project_source(c, project)
        shared = executor_nodes.server_status(c)
        devices = all_rows(c, """SELECT id,name,platform,state,epoch,capabilities,last_seen_at,created_at
            FROM lead.local_executor_devices d WHERE NOT EXISTS(SELECT 1 FROM lead.executor_node_grants g
                WHERE g.device_id=d.id AND g.tenant_id=d.tenant_id AND g.agent_id=d.agent_id)
            ORDER BY created_at DESC""")
        requests = all_rows(c, """SELECT id,task_id,state,thread_id,turn_id,usage,created_at,updated_at,
                scope->'execution_route' AS execution_route
            FROM lead.local_executor_requests ORDER BY created_at DESC LIMIT 30""")
        pending = all_rows(c, """SELECT e.event_id,e.request_id,e.method,e.params,e.created_at
            FROM lead.local_executor_events e JOIN lead.local_executor_requests r ON r.id=e.request_id
            WHERE r.state NOT IN ('completed','failed','interrupted','execution_unknown','canceled')
            AND e.method='server/request' AND NOT EXISTS(SELECT 1 FROM lead.local_executor_commands c
                WHERE c.request_id=e.request_id AND c.kind='respond' AND c.payload->>'id'=e.params->>'id')
            ORDER BY e.created_at DESC LIMIT 20""")
    for device in devices:
        device["online"] = _online(device)
        device['execution_source'] = 'personal_v1'
    for request in requests:
        request['execution_source'] = execution_source({'execution_route': request.pop('execution_route', None)})
    return {"agent_id": str(aid), "mode": 'legacy' if source == 'legacy' else 'codex',
            "device_id": project.get('device_id') if project and source == 'personal_v1' else None,
            'source': source, 'execution_source': source,
            'execution_override': (project or {}).get('execution_override', 'inherit'), 'shared_node': shared,
            "devices": devices, "requests": requests, "pending_inputs": pending, "model": MODEL, "effort": EFFORT}


def _enqueue(c, request, kind, payload, key):
    fingerprint = _digest(_encoded({"kind": kind, "payload": payload}))
    old = one(c, "SELECT * FROM lead.local_executor_commands WHERE request_id=%s AND command_key=%s", (request["id"], key))
    if old:
        if old["command_hash"] != fingerprint:
            raise ValueError("executor_command_conflict")
        return old
    return one(c, """INSERT INTO lead.local_executor_commands(tenant_id,agent_id,request_id,command_key,
        command_hash,kind,payload) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
               (request["tenant_id"], request["agent_id"], request["id"], key, fingerprint, kind, j(payload)))


def command(aid, request_id, kind, payload, command_id=None):
    if kind not in {"steer", "interrupt", "respond"}:
        raise ValueError("executor_command_invalid")
    with tx(str(aid)) as c:
        agent = require_agent(c, aid, True)
        row = _request(c, request_id, lock=True)
        if row["state"] in TERMINAL:
            raise ValueError("executor_request_terminal")
        if kind == "interrupt":
            # Fence tools immediately. An already-submitted action's late
            # evidence is still processed through the original business path.
            c.execute("UPDATE lead.local_executor_requests SET scope=scope||%s,updated_at=now() WHERE id=%s",
                      (j({"canceled": True}), row["id"]))
        elif row["scope"].get("control_version") != agent["control_version"]:
            raise ValueError("executor_scope_changed")
        if kind == "respond":
            pending = one(c, """SELECT event_id,params FROM lead.local_executor_events WHERE request_id=%s
                AND method='server/request' AND params->>'id'=%s""", (row["id"], str(payload.get("id"))))
            if not pending:
                raise ValueError("executor_response_unavailable")
            if execution_source(row['scope']) == 'managed_node':
                pending_method = (pending.get('params') or {}).get('method')
                if not isinstance(pending_method, str) or pending_method in MANAGED_APPROVAL_METHODS:
                    raise ValueError('executor_permission_escalation_forbidden')
        item = _enqueue(c, row, kind, payload, str(command_id or uuid4()))
        audit(c, str(aid), "executor.command", {"request_id": str(request_id), "kind": kind})
        return {"id": str(item["id"]), "request_id": str(request_id), "kind": kind}


def poll(bearer, epoch, *, instance_id=None, capabilities=None):
    claims = _claims(bearer, "device")
    with tenant_scope(claims["tenant_id"], None), tx(claims["agent_id"]) as c:
        device = _device(c, claims, bearer, epoch)
        if instance_id:
            if len(instance_id) > 160:
                raise ValueError("executor_instance_invalid")
            if device["instance_id"] and device["instance_id"] != instance_id:
                device["epoch"] += 1
            device["instance_id"] = instance_id
        c.execute("""UPDATE lead.local_executor_devices SET epoch=%s,instance_id=%s,last_seen_at=now(),
            capabilities=%s WHERE id=%s""", (device["epoch"], device["instance_id"],
                                            j(capabilities if capabilities is not None else device["capabilities"]), device["id"]))
        # Only requests that have never been assigned may attach to this
        # preferred device. Existing ones retain their original device.
        c.execute("""UPDATE lead.local_executor_requests r SET device_id=%s WHERE r.device_id IS NULL AND r.state='queued'
            AND coalesce(r.scope#>>'{execution_route,source}','personal_v1')='personal_v1'
            AND EXISTS(SELECT 1 FROM lead.local_executor_projects p WHERE p.agent_id=r.agent_id
                AND p.tenant_id=r.tenant_id AND p.mode='codex' AND p.device_id=%s)""", (device["id"], device["id"]))
        return poll_device(c, device)


def poll_device(c, device, *, source='personal_v1', grant_id=None, grant_generation=None):
    """A preauthenticated device keeps the original business fence and source."""
    parameters = _source_parameters(source, grant_id, grant_generation)
    aid = str(device['agent_id'])
    agent = require_agent(c, aid)
    active = all_rows(c, """SELECT r.* FROM lead.local_executor_requests r WHERE r.device_id=%s
        AND r.state NOT IN ('completed','failed','interrupted','execution_unknown','canceled') AND """
        + _SOURCE_FILTER, (device['id'], *parameters))
    from .admin_settings import effective
    settings, _ = effective(c, 'models', aid)
    for request in active:
        if not _source_matches(request, source, grant_id, grant_generation):
            continue
        scope = request['scope']
        changed = (scope.get('canceled') or scope.get('control_version') != agent['control_version']
            or request['payload'].get('web_search') == 'live' and not settings['tools']['public_web'])
        if request['task_id']:
            task = one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (request['task_id'],))
            runtime = (task.get('result') or {}).get('runtime', {}) if task else {}
            changed = changed or not task or task['state'] == 'canceled' or (
                task['input'].get('task_generation', 1) != scope.get('task_generation')) or (
                task['state'] != 'succeeded' and scope.get('attempt_token') is not None
                and runtime.get('attempt_token') != scope['attempt_token'])
        if changed:
            c.execute('UPDATE lead.local_executor_requests SET scope=scope||%s WHERE id=%s',
                (j({'canceled': True}), request['id']))
            # A lost ACK never proves the original run did not start.
            _enqueue(c, request, 'interrupt', {}, 'business-scope-fence')
    rows = all_rows(c, """SELECT c.id,c.kind,c.request_id,c.payload,r.scope AS request_scope
        FROM lead.local_executor_commands c JOIN lead.local_executor_requests r
        ON r.id=c.request_id AND r.agent_id=c.agent_id AND r.tenant_id=c.tenant_id
        WHERE r.device_id=%s AND c.acknowledged_at IS NULL AND r.state NOT IN
            ('completed','failed','interrupted','execution_unknown','canceled') AND """
        + _SOURCE_FILTER + ' ORDER BY c.created_at,c.id LIMIT 30', (device['id'], *parameters))
    commands = [{'id': str(row['id']), 'kind': row['kind'], 'request_id': str(row['request_id']),
        'payload': row['payload']} for row in rows
        if _source_matches({'scope': row['request_scope']}, source, grant_id, grant_generation)
        and not (source == 'managed_node' and row['kind'] == 'run' and row['request_scope'].get('canceled'))]
    return {'epoch': device['epoch'], 'commands': commands}


def _thread_values(params):
    if not isinstance(params, dict) or any(params.get(key) is not None and not isinstance(params[key], dict)
            for key in ('thread', 'turn')):
        raise ValueError('executor_payload_invalid')
    thread = params.get("threadId") or params.get("thread_id") or (params.get("thread") or {}).get("id")
    turn = params.get("turnId") or params.get("turn_id") or (params.get("turn") or {}).get("id")
    if any(value is not None and not isinstance(value, str) for value in (thread, turn)):
        raise ValueError('executor_payload_invalid')
    return thread, turn


def event_transition(request, method, params):
    """The shared native recovery codec; no DB, identity or business completion."""
    thread, turn = _thread_values(params)
    identity_event = method in {'request/started', 'result'}
    if identity_event and (
            thread and request.get('thread_id') and thread != request['thread_id']
            or turn and request.get('turn_id') and turn != request['turn_id']):
        raise ValueError('executor_result_conflict')
    if method == 'result':
        if params.get('state') not in TERMINAL:
            raise ValueError('executor_result_invalid')
        reconciled = (request['state'] == 'execution_unknown' and params.get('reconciled') is True
            and params.get('state') in {'completed', 'failed', 'interrupted'}
            and request.get('thread_id') and params.get('thread_id') == request['thread_id']
            and (not request.get('turn_id') or params.get('turn_id') == request['turn_id']))
        if request['state'] in TERMINAL and request.get('result') != params and not reconciled:
            raise ValueError('executor_result_conflict')
    primary = not request.get('thread_id') or not thread or thread == request['thread_id']
    transition = {'thread_id': request.get('thread_id') or (thread if identity_event and primary else None),
        'turn_id': request.get('turn_id') or (turn if identity_event and primary else None),
        'ack_run': False, 'ack_all': False}
    if method == 'command/ack':
        transition['command_id'] = params.get('command_id')
    elif method == 'thread/tokenUsage/updated' and request.get('thread_id') and thread == request['thread_id']:
        transition['usage'] = params.get('tokenUsage') or params
    elif method == 'result':
        transition.update(state=params['state'], result=params, ack_all=True)
    elif request['state'] not in TERMINAL:
        waiting = method in {'server/request', 'item/tool/requestUserInput', *MANAGED_APPROVAL_METHODS}
        transition.update(state='waiting_input' if waiting else 'running', ack_run=True)
    return transition


def events(bearer, epoch, entries):
    claims = _claims(bearer, "device")
    with tenant_scope(claims["tenant_id"], None), tx(claims["agent_id"]) as c:
        device = _device(c, claims, bearer, epoch)
        return device_events(c, device, epoch, entries)


def device_events(c, device, epoch, entries, *, source='personal_v1', grant_id=None, grant_generation=None):
    """Persist only events assigned to this authenticated source/device/grant."""
    _source_parameters(source, grant_id, grant_generation)
    if type(epoch) is not int or device.get('epoch') != epoch:
        raise ValueError('executor_epoch_changed')
    accepted = []
    for item in entries:
        request = _request(c, item['request_id'], device, lock=True)
        _require_request_source(request, source, grant_id, grant_generation)
        fingerprint = _digest(_encoded(item))
        old = one(c, 'SELECT * FROM lead.local_executor_events WHERE event_id=%s', (item['id'],))
        if old:
            if old['event_hash'] != fingerprint:
                raise ValueError('executor_event_conflict')
            accepted.append(item['id'])
            continue
        method, params = item['method'], item.get('params') or {}
        transition = event_transition(request, method, params)
        c.execute("""INSERT INTO lead.local_executor_events(tenant_id,agent_id,request_id,event_id,event_hash,method,params)
            VALUES(%s,%s,%s,%s,%s,%s,%s)""", (device['tenant_id'], device['agent_id'], request['id'],
                item['id'], fingerprint, method, j(params)))
        c.execute("""UPDATE lead.local_executor_requests SET thread_id=coalesce(thread_id,%s),
            turn_id=coalesce(turn_id,%s),updated_at=now() WHERE id=%s""",
            (transition['thread_id'], transition['turn_id'], request['id']))
        if 'command_id' in transition:
            c.execute("""UPDATE lead.local_executor_commands SET acknowledged_at=coalesce(acknowledged_at,now())
                WHERE request_id=%s AND id=%s""", (request['id'], transition['command_id']))
        if 'usage' in transition:
            c.execute('UPDATE lead.local_executor_requests SET usage=%s WHERE id=%s',
                (j(transition['usage']), request['id']))
        if 'result' in transition:
            c.execute("""UPDATE lead.local_executor_requests SET state=%s,result=%s,completed_at=now()
                WHERE id=%s""", (transition['state'], j(transition['result']), request['id']))
        elif 'state' in transition:
            c.execute('UPDATE lead.local_executor_requests SET state=%s WHERE id=%s',
                (transition['state'], request['id']))
        if transition['ack_all']:
            c.execute("""UPDATE lead.local_executor_commands SET acknowledged_at=coalesce(acknowledged_at,now())
                WHERE request_id=%s""", (request['id'],))
        elif transition['ack_run']:
            c.execute("""UPDATE lead.local_executor_commands SET acknowledged_at=coalesce(acknowledged_at,now())
                WHERE request_id=%s AND kind='run'""", (request['id'],))
        accepted.append(item['id'])
    c.execute('UPDATE lead.local_executor_devices SET last_seen_at=now() WHERE id=%s', (device['id'],))
    return {"accepted": accepted, "epoch": epoch}


def tool_request(c, device, request_id, name, *, source='personal_v1', grant_id=None, grant_generation=None):
    """Validate the original tool/Task fence after transport identity validation."""
    _source_parameters(source, grant_id, grant_generation)
    request = _request(c, request_id, device)
    _require_request_source(request, source, grant_id, grant_generation)
    if request['state'] in TERMINAL:
        raise ValueError('executor_request_terminal')
    if name not in {tool.get('name') for tool in request['payload'].get('tools', [])}:
        raise ValueError('executor_tool_not_declared')
    agent = require_agent(c, str(device['agent_id']))
    if request['scope'].get('canceled') or request['scope'].get('control_version') != agent['control_version']:
        raise ValueError('executor_scope_changed')
    if request['task_id']:
        task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (request['task_id'],))
        runtime = (task.get('result') or {}).get('runtime', {}) if task else {}
        if (not task or task['state'] not in {'running', 'reconciling'}
                or task['input'].get('task_generation', 1) != request['scope'].get('task_generation')
                or runtime.get('attempt_token') != request['scope'].get('attempt_token')):
            raise ValueError('executor_scope_changed')
    return request


async def call_tool(bearer, epoch, request_id, name, arguments):
    claims = _claims(bearer, "device")
    with tenant_scope(claims["tenant_id"], None):
        with tx(claims["agent_id"]) as c:
            device = _device(c, claims, bearer, epoch)
            request = tool_request(c, device, request_id, name)
        # The adapter independently revalidates the original action/connection
        # at its actual barrier; do not keep a DB lock across browser/network IO.
        from .local_executor_tools import call
        return await call(device, request, name, arguments)
