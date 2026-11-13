"""Durable transport for a user's official Codex runtime.

The transport owns neither business completion nor external-send identity. In
particular, an uncertain app-server start is never silently issued again.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from .config import CONFIG
from .db import all_rows, audit, j, one, require_agent, tx
from .scope import current_tenant, tenant_scope

MODEL = "gpt-6.1-sol"
EFFORT = "medium"
TERMINAL = {"completed", "failed", "interrupted", "execution_unknown", "canceled"}
ONLINE_SECONDS = 90


def _now():
    return datetime.now(timezone.utc)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


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
    if aid is None:
        return default_agent(connection=connection) is not None
    with (nullcontext(connection) if connection is not None else tx(str(aid))) as c:
        row = one(c, "SELECT mode FROM lead.local_executor_projects WHERE agent_id=%s", (aid,))
        return bool(row and row["mode"] == "codex")


def configured(aid=None):
    aid = aid or default_agent()
    if not aid:
        return False
    with tx(str(aid)) as c:
        return _online(one(c, """SELECT d.* FROM lead.local_executor_projects p JOIN lead.local_executor_devices d
            ON d.id=p.device_id AND d.tenant_id=p.tenant_id AND d.agent_id=p.agent_id
            WHERE p.agent_id=%s AND p.mode='codex'""", (aid,)))


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

    aid = str(aid)
    device_id = str(uuid4())
    expires = _now()+timedelta(minutes=5)
    code = _token("pair", tenant_id=current_tenant(), agent_id=aid, device_id=device_id,
                  nonce=secrets.token_urlsafe(24), expires=int(expires.timestamp()))
    with tx(aid) as c:
        require_agent(c, aid, True)
        c.execute("""INSERT INTO lead.local_executor_devices(id,tenant_id,agent_id,pairing_hash,pairing_expires_at)
            VALUES(%s,%s,%s,%s,%s)""", (device_id, current_tenant(), aid, _digest(code), expires))
        audit(c, aid, "executor.pairing_created", {"device_id": device_id})
    return {"code": code, "expires_at": expires.isoformat(), "device_id": device_id,
            "connect_command": "lead-executor pair --url "+public_origin()+" --code "+code}


def pair(code, name, platform, capabilities):
    claims = _claims(code, "pair")
    with tenant_scope(claims["tenant_id"], None), tx(claims["agent_id"]) as c:
        require_agent(c, claims["agent_id"], True)
        device = one(c, "SELECT * FROM lead.local_executor_devices WHERE id=%s FOR UPDATE", (claims["device_id"],))
        if (not device or device["state"] != "pairing" or device["pairing_hash"] != _digest(code)
                or device["pairing_expires_at"] <= _now()):
            raise ValueError("pairing_unavailable")
        bearer = _token("device", tenant_id=claims["tenant_id"], agent_id=claims["agent_id"],
                        device_id=claims["device_id"], nonce=secrets.token_urlsafe(32),
                        expires=int(time.time())+30*86400)
        c.execute("""UPDATE lead.local_executor_devices SET state='active',name=%s,platform=%s,
            capabilities=%s,token_hash=%s,pairing_hash=NULL,pairing_expires_at=NULL,last_seen_at=now() WHERE id=%s""",
                  (name[:160], platform[:80], j(capabilities), _digest(bearer), device["id"]))
        c.execute("""INSERT INTO lead.local_executor_projects(tenant_id,agent_id,mode,device_id)
            VALUES(%s,%s,'codex',%s) ON CONFLICT(tenant_id,agent_id) DO UPDATE SET
            mode='codex',device_id=excluded.device_id,updated_at=now()""",
                  (claims["tenant_id"], claims["agent_id"], device["id"]))
        audit(c, claims["agent_id"], "executor.paired", {"device_id": str(device["id"]), "platform": platform[:80]})
        return {"device_id": str(device["id"]), "token": bearer, "epoch": device["epoch"],
                "agent_id": claims["agent_id"], "tenant_id": claims["tenant_id"], "model": MODEL, "effort": EFFORT}


def set_mode(aid, mode):
    if mode not in {"legacy", "codex"}:
        raise ValueError("executor_mode_invalid")
    with tx(str(aid)) as c:
        require_agent(c, aid, True)
        c.execute("""INSERT INTO lead.local_executor_projects(tenant_id,agent_id,mode) VALUES(%s,%s,%s)
            ON CONFLICT(tenant_id,agent_id) DO UPDATE SET mode=excluded.mode,updated_at=now()""",
                  (current_tenant(), aid, mode))
        audit(c, str(aid), "executor.mode_changed", {"mode": mode})
    return {"mode": mode}


def revoke(aid, device_id):
    with tx(str(aid)) as c:
        require_agent(c, aid, True)
        row = one(c, """UPDATE lead.local_executor_devices SET state='revoked',token_hash=NULL,
            pairing_hash=NULL,revoked_at=now(),epoch=epoch+1 WHERE id=%s RETURNING id""", (device_id,))
        if not row:
            raise LookupError("executor_device_unavailable")
        audit(c, str(aid), "executor.revoked", {"device_id": str(device_id)})
    return {"revoked": True}


def _device(c, claims, bearer, epoch=None):
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
    if not row or (device and row["device_id"] != device["id"]):
        raise ValueError("executor_request_unavailable")
    return row


def request_by_key(aid, request_key):
    with tx(str(aid)) as c:
        require_agent(c, aid)
        return one(c, "SELECT * FROM lead.local_executor_requests WHERE request_key=%s", (request_key,))


def submit(aid, request_key, payload, *, session_key, task_id=None, resume=False):
    aid = str(aid or default_agent() or "")
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
        session = one(c, """SELECT device_id FROM lead.local_executor_requests WHERE session_key=%s
            AND device_id IS NOT NULL ORDER BY created_at,id LIMIT 1""", (session_key,))
        task_binding = one(c, "SELECT device_id FROM lead.local_executor_requests WHERE task_id=%s ORDER BY created_at,id LIMIT 1", (task_id,)) if task_id else None
        if not project or project["mode"] != "codex" and not (session or task_binding):
            raise ValueError("executor_not_configured")
        device_id = session["device_id"] if session else task_binding["device_id"] if task_binding else project["device_id"]
        scope = {"control_version": agent["control_version"]}
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
    with tx(str(aid)) as c:
        require_agent(c, aid)
        project = one(c, "SELECT mode,device_id FROM lead.local_executor_projects WHERE agent_id=%s", (aid,))
        devices = all_rows(c, """SELECT id,name,platform,state,epoch,capabilities,last_seen_at,created_at
            FROM lead.local_executor_devices ORDER BY created_at DESC""")
        requests = all_rows(c, """SELECT id,task_id,state,thread_id,turn_id,usage,created_at,updated_at
            FROM lead.local_executor_requests ORDER BY created_at DESC LIMIT 30""")
        pending = all_rows(c, """SELECT e.event_id,e.request_id,e.method,e.params,e.created_at
            FROM lead.local_executor_events e JOIN lead.local_executor_requests r ON r.id=e.request_id
            WHERE r.state NOT IN ('completed','failed','interrupted','execution_unknown','canceled')
            AND e.method='server/request' AND NOT EXISTS(SELECT 1 FROM lead.local_executor_commands c
                WHERE c.request_id=e.request_id AND c.kind='respond' AND c.payload->>'id'=e.params->>'id')
            ORDER BY e.created_at DESC LIMIT 20""")
    for device in devices:
        device["online"] = _online(device)
    return {"agent_id": str(aid), "mode": project["mode"] if project else "legacy", "device_id": project["device_id"] if project else None,
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
            pending = one(c, """SELECT event_id FROM lead.local_executor_events WHERE request_id=%s
                AND method='server/request' AND params->>'id'=%s""", (row["id"], str(payload.get("id"))))
            if not pending:
                raise ValueError("executor_response_unavailable")
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
            AND EXISTS(SELECT 1 FROM lead.local_executor_projects p WHERE p.agent_id=r.agent_id
                AND p.tenant_id=r.tenant_id AND p.mode='codex' AND p.device_id=%s)""", (device["id"], device["id"]))
        agent = require_agent(c, claims["agent_id"])
        active = all_rows(c, "SELECT * FROM lead.local_executor_requests WHERE device_id=%s AND state NOT IN ('completed','failed','interrupted','execution_unknown','canceled')", (device["id"],))
        from .admin_settings import effective
        settings, _ = effective(c, "models", claims["agent_id"])
        for request in active:
            scope = request["scope"]
            changed = (scope.get("canceled") or scope.get("control_version") != agent["control_version"]
                or request["payload"].get("web_search") == "live" and not settings["tools"]["public_web"])
            if request["task_id"]:
                task = one(c, "SELECT state,input,result FROM lead.tasks WHERE id=%s", (request["task_id"],))
                runtime = (task.get("result") or {}).get("runtime", {}) if task else {}
                changed = changed or not task or task["state"] == "canceled" or (
                    task["state"] != "succeeded" and scope.get("attempt_token") is not None
                    and runtime.get("attempt_token") != scope["attempt_token"])
            if changed:
                c.execute("UPDATE lead.local_executor_requests SET scope=scope||%s WHERE id=%s", (j({"canceled": True}), request["id"]))
                # A queued row can already have reached the device with a lost
                # ACK. Send an original-request interrupt, never infer no start.
                _enqueue(c, request, "interrupt", {}, "business-scope-fence")
        rows = all_rows(c, """SELECT c.id,c.kind,c.request_id,c.payload FROM lead.local_executor_commands c
            JOIN lead.local_executor_requests r ON r.id=c.request_id AND r.agent_id=c.agent_id AND r.tenant_id=c.tenant_id
            WHERE r.device_id=%s AND c.acknowledged_at IS NULL AND r.state NOT IN
                ('completed','failed','interrupted','execution_unknown','canceled')
            ORDER BY c.created_at,c.id LIMIT 30""", (device["id"],))
        return {"epoch": device["epoch"], "commands": [{**item, "id": str(item["id"]), "request_id": str(item["request_id"])} for item in rows]}


def _thread_values(params):
    thread = params.get("threadId") or params.get("thread_id") or (params.get("thread") or {}).get("id")
    turn = params.get("turnId") or params.get("turn_id") or (params.get("turn") or {}).get("id")
    return thread, turn


def events(bearer, epoch, entries):
    claims = _claims(bearer, "device")
    accepted = []
    with tenant_scope(claims["tenant_id"], None), tx(claims["agent_id"]) as c:
        device = _device(c, claims, bearer, epoch)
        for item in entries:
            request = _request(c, item["request_id"], device, lock=True)
            fingerprint = _digest(_encoded(item))
            old = one(c, "SELECT * FROM lead.local_executor_events WHERE event_id=%s", (item["id"],))
            if old:
                if old["event_hash"] != fingerprint:
                    raise ValueError("executor_event_conflict")
                accepted.append(item["id"])
                continue
            method, params = item["method"], item.get("params") or {}
            if method == "result":
                if params.get("state") not in TERMINAL:
                    raise ValueError("executor_result_invalid")
                reconciled = (request["state"] == "execution_unknown" and params.get("reconciled") is True
                              and params.get("state") in {"completed", "failed", "interrupted"}
                              and request.get("thread_id") and params.get("thread_id") == request["thread_id"]
                              and (not request.get("turn_id") or params.get("turn_id") == request["turn_id"]))
                if request["state"] in TERMINAL and request.get("result") != params and not reconciled:
                    raise ValueError("executor_result_conflict")
            c.execute("""INSERT INTO lead.local_executor_events(tenant_id,agent_id,request_id,event_id,event_hash,method,params)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""", (claims["tenant_id"], claims["agent_id"], request["id"],
                                                   item["id"], fingerprint, method, j(params)))
            thread, turn = _thread_values(params)
            c.execute("""UPDATE lead.local_executor_requests SET thread_id=coalesce(%s,thread_id),
                turn_id=coalesce(%s,turn_id),updated_at=now() WHERE id=%s""", (thread, turn, request["id"]))
            if method == "command/ack":
                c.execute("""UPDATE lead.local_executor_commands SET acknowledged_at=coalesce(acknowledged_at,now())
                    WHERE request_id=%s AND id=%s""", (request["id"], params.get("command_id")))
            elif method == "thread/tokenUsage/updated":
                c.execute("UPDATE lead.local_executor_requests SET usage=%s WHERE id=%s",
                          (j(params.get("tokenUsage") or params), request["id"]))
            elif method == "result":
                c.execute("""UPDATE lead.local_executor_requests SET state=%s,result=%s,completed_at=now()
                    WHERE id=%s""", (params["state"], j(params), request["id"]))
                c.execute("UPDATE lead.local_executor_commands SET acknowledged_at=coalesce(acknowledged_at,now()) WHERE request_id=%s",
                          (request["id"],))
            elif request["state"] not in TERMINAL:
                waiting = method in {"server/request", "item/tool/requestUserInput", "item/commandExecution/requestApproval",
                                     "item/fileChange/requestApproval", "item/permissions/requestApproval"}
                c.execute("UPDATE lead.local_executor_requests SET state=%s WHERE id=%s",
                          ("waiting_input" if waiting else "running", request["id"]))
                c.execute("""UPDATE lead.local_executor_commands SET acknowledged_at=coalesce(acknowledged_at,now())
                    WHERE request_id=%s AND kind='run'""", (request["id"],))
            accepted.append(item["id"])
        c.execute("UPDATE lead.local_executor_devices SET last_seen_at=now() WHERE id=%s", (device["id"],))
    return {"accepted": accepted, "epoch": epoch}


async def call_tool(bearer, epoch, request_id, name, arguments):
    claims = _claims(bearer, "device")
    with tenant_scope(claims["tenant_id"], None):
        with tx(claims["agent_id"]) as c:
            device = _device(c, claims, bearer, epoch)
            request = _request(c, request_id, device)
            if request["state"] in TERMINAL:
                raise ValueError("executor_request_terminal")
            if name not in {tool.get("name") for tool in request["payload"].get("tools", [])}:
                raise ValueError("executor_tool_not_declared")
            agent = require_agent(c, claims["agent_id"])
            if request["scope"].get("canceled") or request["scope"].get("control_version") != agent["control_version"]:
                raise ValueError("executor_scope_changed")
            if request["task_id"]:
                task = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (request["task_id"],))
                runtime = (task.get("result") or {}).get("runtime", {}) if task else {}
                if (not task or task["state"] not in {"running", "reconciling"}
                        or task["input"].get("task_generation", 1) != request["scope"].get("task_generation")
                        or runtime.get("attempt_token") != request["scope"].get("attempt_token")):
                    raise ValueError("executor_scope_changed")
        # The adapter independently revalidates the original action/connection
        # at its actual barrier; do not keep a DB lock across browser/network IO.
        from .local_executor_tools import call
        return await call(device, request, name, arguments)
