"""Request identity is separate from trusted Agent execution identity."""

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .config import TENANT

_tenant = ContextVar("lead_tenant", default=None)
_principal = ContextVar("lead_principal", default=None)
_service = ContextVar("lead_service_execution", default=False)
_UNCHANGED = object()


def current_tenant():
    return _tenant.get() or TENANT


def principal():
    return _principal.get()


def actor_ref():
    who = principal()
    if not who or who.get("issuer") == "local":
        return "local"
    return (
        "oidc:"
        + hashlib.sha256((who["issuer"] + "\x1f" + who["subject"]).encode()).hexdigest()
    )


def trusted_execution():
    return _service.get()


@contextmanager
def tenant_scope(tenant, identity=_UNCHANGED):
    tenant_token = _tenant.set(str(tenant))
    principal_token = _principal.set(
        _principal.get() if identity is _UNCHANGED else identity
    )
    try:
        yield
    finally:
        _principal.reset(principal_token)
        _tenant.reset(tenant_token)


@contextmanager
def service_scope():
    handle = _service.set(True)
    try:
        yield
    finally:
        _service.reset(handle)


def scoped_activity(fn):
    @wraps(fn)
    async def run(args):
        from .db import one, tx
        from .identity_store import agent_tenant
        from .telemetry import span

        with tenant_scope(agent_tenant(args["agent_id"]), None), service_scope():
            parent = args.get("trace_parent")
            table, identifier = (
                ("chat_jobs", args.get("job_id"))
                if args.get("job_id")
                else ("tasks", args.get("task_id"))
            )
            if identifier and not parent:
                with tx(args["agent_id"]) as c:
                    row = one(
                        c,
                        "SELECT trace_parent FROM lead." + table + " WHERE id=%s",
                        (identifier,),
                    )
                    parent = row["trace_parent"] if row else None
            with span(
                fn.__name__, parent=parent, attributes={"lead.operation": fn.__name__}
            ):
                return await fn(args)

    return run


def scoped_agent_method(fn):
    @wraps(fn)
    async def run(self, aid, *args, **kwargs):
        from .identity_store import agent_tenant

        with tenant_scope(agent_tenant(aid), None), service_scope():
            return await fn(self, aid, *args, **kwargs)

    return run
