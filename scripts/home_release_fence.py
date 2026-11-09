#!/usr/local/lib/lead-home-release/venv/bin/python3 -IB
"""A separate systemd process holds PostgreSQL admission locks across an application cutover."""

import hashlib
import os
import pwd
import re
import socket
import sys
import time

import psycopg
from psycopg import sql

# These are the durable admission points for business work. Backups write separate tables.
TABLES = ("actions", "agents", "browser_runs", "browser_sessions", "chat_jobs", "notification_deliveries",
          "profile_leases", "profile_maintenance", "takeover_sessions", "tasks")
BUSY = """SELECT EXISTS (SELECT FROM lead.agents WHERE desired_state NOT IN ('draft','paused','archived'))
    OR EXISTS (SELECT FROM lead.actions WHERE state IN ('prepared','dispatching','unknown'))
    OR EXISTS (SELECT FROM lead.browser_runs WHERE state='running')
    OR EXISTS (SELECT FROM lead.browser_sessions WHERE state<>'closed')
    OR EXISTS (SELECT FROM lead.profile_leases WHERE state IN ('held','uncertain'))
    OR EXISTS (SELECT FROM lead.profile_maintenance WHERE state='active' AND expires_at>now())
    OR EXISTS (SELECT FROM lead.chat_jobs WHERE status IN ('pending','processing'))
    OR EXISTS (SELECT FROM lead.tasks WHERE state IN ('queued','running','reconciling'))
    OR EXISTS (SELECT FROM lead.notification_deliveries WHERE state IN ('sending','unknown'))
    OR EXISTS (SELECT FROM lead.takeover_sessions WHERE state IN ('requested','active','ending')
               AND expires_at>now())"""


def fence_name(old, new):
    if not all(re.fullmatch("[0-9a-f]{40}", s) for s in (old, new)):
        raise ValueError("invalid revision pair")
    return "lead-release-" + hashlib.sha256((old + new).encode()).hexdigest()[:48]


def acquire(conn):
    if not conn.execute("SELECT pg_try_advisory_xact_lock(5713964323482947)").fetchone()[0]:
        raise ValueError("another maintenance session is active")
    conn.execute("SET LOCAL lock_timeout='5s'")
    conn.execute("SET LOCAL row_security=off")
    conn.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE").format(sql.SQL(",").join(
        sql.Identifier("lead", table) for table in TABLES)))
    if conn.execute(BUSY).fetchone()[0]:
        raise ValueError("operations have not drained")


def held(conn, name):
    rows = conn.execute("""SELECT a.pid, array_agg(c.relname::text ORDER BY c.relname::text)
        FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid AND l.database=a.datid
        JOIN pg_class c ON c.oid=l.relation JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE a.datname=current_database() AND a.usename=current_user AND a.application_name=%s
          AND l.locktype='relation' AND l.mode='ShareLock' AND l.granted AND n.nspname='lead'
          AND c.relname=ANY(%s)
        GROUP BY a.pid""", (name, list(TABLES))).fetchall()
    if len(rows) != 1 or rows[0][1] != sorted(TABLES):
        raise ValueError("active revision-bound maintenance lock unavailable")


def main(args):
    holding = len(args) == 2 and args[0] == "--hold"
    old, new = args[1].split("-") if holding else args
    name = fence_name(old, new)
    # The installer provisions a separate DB identity on the lead-only database.
    if os.geteuid() == 0:
        account = pwd.getpwnam("lead-release-fence")
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    os.environ["PGSERVICEFILE"] = "/etc/lead-home-release/pg_service.conf"
    with psycopg.connect(service="lead-release-fence", application_name=name if holding else "lead-release-check",
                         connect_timeout=4) as conn:
        if not holding:
            conn.execute("SET TRANSACTION READ ONLY")
            held(conn, name)
            return
        acquire(conn)
        notify = os.environ["NOTIFY_SOCKET"]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"READY=1", "\0" + notify[1:] if notify.startswith("@") else notify)
        # The dispatcher's death does not release the fence. systemd stop closes this session.
        while True:
            time.sleep(3)
            conn.execute("SELECT 1")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        sys.exit("maintenance fence unavailable or business work outstanding")
