"""Narrow identity resolver role. The business role has no session/membership writes."""

import hashlib
import secrets
from contextlib import contextmanager
from functools import lru_cache
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .config import database_kwargs


@contextmanager
def auth_tx():
    with psycopg.connect(**database_kwargs(auth=True), row_factory=dict_row) as c:
        yield c


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


@lru_cache(maxsize=4096)
def agent_tenant(aid):
    aid = str(UUID(str(aid)))
    with auth_tx() as c:
        tenant = c.execute(
            "SELECT lead.execution_agent_tenant(%s) AS id", (aid,)
        ).fetchone()["id"]
    if not tenant:
        raise LookupError("Agent 不存在")
    return str(tenant)


def execution_tenants():
    with auth_tx() as c:
        return [
            str(row["id"])
            for row in c.execute("SELECT * FROM lead.execution_tenants()").fetchall()
        ]


def public_tenant(kind, reference):
    with auth_tx() as c:
        result = c.execute(
            "SELECT lead.public_ref_tenant(%s,%s) AS id", (kind, str(reference))
        ).fetchone()["id"]
    return str(result) if result else None


def lookup_session(secret):
    if not secret or len(secret) > 100 or not secret.startswith("lead_s_"):
        return None
    with auth_tx() as c:
        row = c.execute(
            "SELECT * FROM lead.auth_session(%s)", (sha(secret),)
        ).fetchone()
    if row:
        row["tenant_id"] = str(row["tenant_id"])
    return row


def login(issuer, subject):
    secret = "lead_s_" + secrets.token_urlsafe(32)
    with auth_tx() as c:
        tenant = c.execute(
            "SELECT lead.auth_enroll(%s,%s) AS id", (issuer, subject)
        ).fetchone()["id"]
        if not tenant:
            raise PermissionError("此工作室成员权限已被撤销")
        if not c.execute(
            "SELECT lead.auth_create_session(%s,%s,%s,%s) AS ok",
            (sha(secret), tenant, issuer, subject),
        ).fetchone()["ok"]:
            raise PermissionError("成员权限不可用")
    return secret
