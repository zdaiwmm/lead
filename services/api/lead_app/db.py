from contextlib import contextmanager
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import TENANT, database_kwargs


def uid():
    return str(uuid4())


def j(value):
    return Jsonb(value)


@contextmanager
def tx(agent_id=None, tenant_id=TENANT):
    if agent_id:
        agent_id = str(UUID(str(agent_id)))
    with psycopg.connect(**database_kwargs(), row_factory=dict_row) as conn:
        conn.execute(
            "SELECT set_config('app.tenant_id',%s,true), set_config('app.agent_id',%s,true)",
            (str(tenant_id), agent_id or ""),
        )
        yield conn


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
    conn.execute(
        "INSERT INTO lead.audit_events(tenant_id,agent_id,actor_type,actor_ref,type,object_ref,payload,trace_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            TENANT,
            agent_id,
            "local_owner",
            "local",
            kind,
            str(agent_id),
            j(payload or {}),
            uid(),
        ),
    )


def emit(conn, agent_id, kind, payload=None):
    conn.execute(
        "INSERT INTO lead.outbox_events(tenant_id,agent_id,aggregate_type,aggregate_id,aggregate_version,event_type,schema_version,payload,trace_id) VALUES(%s,%s,%s,%s,1,%s,1,%s,%s)",
        (TENANT, agent_id, "agent", agent_id, kind, j(payload or {}), uid()),
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
            TENANT,
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
        (TENANT, agent_id, title, detail, level, task_id),
    )
