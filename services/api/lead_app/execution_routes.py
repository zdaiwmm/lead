"""Resolve registered transports; callers cannot supply an arbitrary RPC URL."""
import json
from urllib.parse import urlsplit

from .account_intents import auth_scope
from .channel_contracts import ExecutionRef
from .config import CONFIG, env
from .db import one, require_agent, tx
from .identity_store import auth_tx


def transports():
    raw = env().get("LEAD_BROWSER_TRANSPORTS")
    values = json.loads(raw) if raw else CONFIG.get("browser_transports",{"shared":"http://127.0.0.1:8089"})
    if not isinstance(values, dict):
        raise ValueError("invalid_transport_registry")
    for key, url in values.items():
        parsed = urlsplit(url)
        if (not isinstance(key, str) or parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1")
                or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("invalid_transport_registry")
    return values


def default_transport(platform):
    raw = env().get("LEAD_BROWSER_ROUTE_DEFAULTS")
    defaults = json.loads(raw) if raw else CONFIG.get("browser_route_defaults",{})
    if not isinstance(defaults,dict):
        raise ValueError("invalid_default_routes")
    return defaults.get(platform,defaults.get("default","shared"))


def register(aid, profile_id, transport_ref="shared", executor_ref="browser", expected_generation=None):
    if transport_ref not in transports():
        raise ValueError("transport_not_installed")
    with tx(aid) as c:
        require_agent(c, aid)
        if not one(c, "SELECT id FROM lead.browser_profiles WHERE id=%s", (profile_id,)):
            raise ValueError("profile_scope")
    with auth_tx() as c:
        auth_scope(c, aid)
        old = c.execute("SELECT * FROM lead.profile_execution_routes WHERE profile_id=%s FOR UPDATE", (profile_id,)).fetchone()
        if old and expected_generation is not None and old["generation"] != expected_generation:
            raise ValueError("route_changed")
        if old and (old["transport_ref"], old["executor_ref"]) == (transport_ref, executor_ref):
            return ExecutionRef.model_validate({k: old[k] for k in ("profile_id","executor_ref","transport_ref")} |
                                               {"route_generation":old["generation"]})
        row = c.execute("""INSERT INTO lead.profile_execution_routes(tenant_id,agent_id,profile_id,executor_ref,transport_ref)
          VALUES(lead.ctx_tenant(),%s,%s,%s,%s) ON CONFLICT(profile_id) DO UPDATE SET
          executor_ref=EXCLUDED.executor_ref,transport_ref=EXCLUDED.transport_ref,
          generation=profile_execution_routes.generation+CASE WHEN
            (profile_execution_routes.executor_ref,profile_execution_routes.transport_ref)
              IS DISTINCT FROM (EXCLUDED.executor_ref,EXCLUDED.transport_ref) THEN 1 ELSE 0 END,
          updated_at=now() RETURNING *""",
          (aid, profile_id, executor_ref, transport_ref)).fetchone()
    return ExecutionRef(profile_id=row["profile_id"], executor_ref=row["executor_ref"],
                        transport_ref=row["transport_ref"], route_generation=row["generation"])


def resolve(aid, *, account_id=None, profile_id=None):
    with tx(aid) as c:
        require_agent(c, aid)
        if account_id:
            account = one(c, "SELECT profile_id FROM lead.social_accounts WHERE id=%s", (account_id,))
            if not account:
                raise ValueError("account_scope")
            if profile_id and str(profile_id) != str(account["profile_id"]):
                raise ValueError("profile_scope")
            profile_id = account["profile_id"]
        if not profile_id:
            raise ValueError("explicit_profile_required")
        row = one(c, "SELECT * FROM lead.profile_execution_routes WHERE profile_id=%s", (profile_id,))
        if not row or row["transport_ref"] not in transports():
            raise ValueError("execution_route_missing")
        return ExecutionRef(profile_id=row["profile_id"], executor_ref=row["executor_ref"],
                            transport_ref=row["transport_ref"], route_generation=row["generation"])


def endpoint(reference):
    return transports()[reference.transport_ref].rstrip("/")


def research_endpoint(aid, run_id):
    with tx(aid) as c:
        require_agent(c,aid)
        return endpoint(request_route(c,aid,"/research/command",{"run_id":str(run_id)}))


def request_route(c, aid, path, data=None):
    """All browser consumers resolve the same explicit server-side reference."""
    from urllib.parse import parse_qs
    data = data or {}
    query = parse_qs(urlsplit(path).query)
    profile = data.get("profile_id")
    if takeover := data.get("takeover") or query.get("takeover", [None])[0]:
        row = one(c, "SELECT profile_id FROM lead.takeover_sessions WHERE id=%s", (takeover,))
        profile = (row or {}).get("profile_id")
    elif account := data.get("account_id"):
        row = one(c, "SELECT profile_id FROM lead.social_accounts WHERE id=%s", (account,))
        profile = (row or {}).get("profile_id")
    elif action := data.get("action_id"):
        original = one(c,"SELECT a.state,a.intent,t.input FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id WHERE a.id=%s",(action,))
        original_ref = (original["intent"].get("execution_ref") or original["input"].get("migration",{}).get("original_execution_ref")) if original else None
        if original and original["state"] in ("dispatching","unknown","confirmed") and original_ref:
            # Reconcile on the original executor even after a route/binding change.
            reference = ExecutionRef.model_validate(original_ref)
            if reference.transport_ref not in transports():
                raise ValueError("original_execution_route_unavailable")
            return reference
        row = one(c, """SELECT s.profile_id FROM lead.actions a JOIN lead.account_targets t ON t.id=a.target_id
            JOIN lead.social_accounts s ON s.id=t.account_id WHERE a.id=%s""", (action,))
        profile = (row or {}).get("profile_id")
    elif task := data.get("task_id"):
        row = one(c, "SELECT input FROM lead.tasks WHERE id=%s", (task,))
        profile = (row or {}).get("input", {}).get("profile_id")
    elif run := data.get("run_id") or (__import__("re").search(r"/research/([0-9a-fA-F-]{36})/",path) or [None,None])[1]:
        row = one(c, "SELECT profile_id FROM lead.browser_runs WHERE id=%s", (run,))
        profile = (row or {}).get("profile_id")
    if not profile:
        raise ValueError("explicit_profile_required")
    route = one(c, "SELECT * FROM lead.profile_execution_routes WHERE profile_id=%s", (profile,))
    if not route or route["transport_ref"] not in transports():
        raise ValueError("execution_route_missing")
    return ExecutionRef(profile_id=route["profile_id"], executor_ref=route["executor_ref"],
                        transport_ref=route["transport_ref"], route_generation=route["generation"])
