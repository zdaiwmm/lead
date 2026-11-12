import json
import time
from contextlib import contextmanager
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import database_kwargs
from .scope import current_tenant, trusted_execution
from .telemetry import trace_id, trace_parent


def uid():
    return str(uuid4())


def j(value):
    return Jsonb(value)


def connect_runtime():
    """Retry transient direct connections only, before a transaction exists."""
    delays = (0.1, 0.3, 0.6)
    for attempt in range(len(delays) + 1):
        try:
            return psycopg.connect(**database_kwargs(), row_factory=dict_row)
        except psycopg.OperationalError as error:
            code = error.sqlstate
            # libpq may return no SQLSTATE during startup, including for bad
            # credentials. Only retry recognizable transport failures then.
            if code:
                transient = code.startswith("08") or code in {"57P01", "57P02", "57P03", "53300"}
            else:
                transient = any(reason in str(error).lower() for reason in (
                    "connection refused", "connection closed", "server closed the connection",
                    "connection reset", "timeout expired", "connection timed out",
                    "temporary failure in name resolution",
                ))
            if not transient or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


@contextmanager
def tx(agent_id=None, tenant_id=None, *, pooled=True):
    from .scope import tenant_scope, trusted_execution

    if agent_id:
        agent_id = str(UUID(str(agent_id)))
    if tenant_id is None and agent_id and trusted_execution():
        from .identity_store import agent_tenant

        tenant_id = agent_tenant(agent_id)
    tenant_id = str(tenant_id or current_tenant())
    from .db_pool import pool

    connection = (
        pool().connection()
        if pooled
        else connect_runtime()
    )
    with (
        tenant_scope(tenant_id),
        connection as conn,
    ):
        conn.execute(
            "SELECT set_config('app.tenant_id',%s,true), set_config('app.agent_id',%s,true),set_config('app.analytics_scope','',true)",
            (tenant_id, agent_id or ""),
        )
        conn.execute("SELECT set_config('app.growth_protocol','1',true)")
        from .scope import principal

        who = principal()
        allowed = who.get("agent_ids") if who else None
        conn.execute(
            "SELECT set_config('app.allowed_agent_ids',%s,true)",
            (json.dumps(allowed) if allowed is not None else "*",),
        )
        yield conn


def pooled_tx(agent_id=None, tenant_id=None):
    return tx(agent_id, tenant_id, pooled=True)


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def all_rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def require_agent(conn, agent_id, lock=False):
    row = one(
        conn,
        "SELECT * FROM lead.agents WHERE id=%s"
        + (" FOR NO KEY UPDATE" if lock else ""),
        (agent_id,),
    )
    if not row or row["desired_state"] == "archived":
        raise LookupError("Agent 不存在")
    return row


def audit(conn, agent_id, kind, payload=None):
    from .scope import principal

    who = principal()
    conn.execute(
        "INSERT INTO lead.audit_events(tenant_id,agent_id,actor_type,actor_ref,type,object_ref,payload,trace_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            current_tenant(),
            agent_id,
            "oidc"
            if who and who.get("issuer") != "local"
            else "service"
            if trusted_execution()
            else "local_owner",
            who["subject"] if who else "internal",
            kind,
            str(agent_id),
            j(payload or {}),
            trace_id(),
        ),
    )


def emit(conn, agent_id, kind, payload=None):
    if kind in {"supervisor.wake","agent.updated"}:
        from .growth.store import enabled as growth_enabled
        agent = one(conn,"SELECT * FROM lead.agents WHERE id=%s",(agent_id,))
        if agent and growth_enabled(agent):
            if kind=="agent.updated":
                return
            kind = "growth.wake"
            payload = {"reason":"configuration_changed","control_version":agent["control_version"],"config_revision":agent["config_revision"]}
    if kind in {"supervisor.wake", "agent.updated"}:
        from .cadence_events import wake_clocks
        wake_clocks(conn, agent_id)
    parent = trace_parent()
    payload = payload or {}
    if parent and kind in ("chat.enqueue", "task.enqueue"):
        table, reference = (
            ("chat_jobs", payload.get("job_id"))
            if kind == "chat.enqueue"
            else ("tasks", payload.get("task_id"))
        )
        if reference:
            conn.execute(
                "UPDATE lead."
                + table
                + " SET trace_parent=coalesce(trace_parent,%s) WHERE id=%s",
                (parent, reference),
            )
    conn.execute(
        "INSERT INTO lead.outbox_events(tenant_id,agent_id,aggregate_type,aggregate_id,aggregate_version,event_type,schema_version,payload,trace_id,trace_parent) VALUES(%s,%s,%s,%s,1,%s,1,%s,%s,%s)",
        (
            current_tenant(),
            agent_id,
            "agent",
            agent_id,
            kind,
            j(payload),
            trace_id(),
            parent,
        ),
    )


def append_message(
    conn, agent_id, role, text, client_id=None, status="complete", cards=None
):
    convo = one(
        conn,
        "UPDATE lead.conversations SET next_seq=next_seq+1 WHERE agent_id=%s AND purpose='main' RETURNING id,next_seq-1 AS seq",
        (agent_id,),
    )
    return one(
        conn,
        "INSERT INTO lead.conversation_messages(tenant_id,agent_id,conversation_id,seq,client_message_id,role,content,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (
            current_tenant(),
            agent_id,
            convo["id"],
            convo["seq"],
            client_id,
            role,
            j({"text": text, "cards": cards or []}),
            status,
        ),
    )


def record(conn, agent_id, title, detail="", level="info", task_id=None):
    conn.execute(
        "INSERT INTO lead.activity(tenant_id,agent_id,title,detail,level,task_id) VALUES(%s,%s,%s,%s,%s,%s)",
        (current_tenant(), agent_id, title, detail, level, task_id),
    )
