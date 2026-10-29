import concurrent.futures
from uuid import uuid4

import psycopg
import pytest
from lead_app import domain, managed_network
from lead_app.config import database_kwargs
from lead_app.db import tx


def test_sticky_pool_atomic_assignment_scope_and_no_secret_disclosure(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aids = [domain.create_agent() for _ in range(3)]
    pool = []
    marker = "private-proxy-password-" + uuid4().hex
    try:
        for n, ip in enumerate(["1.1.1.1", "8.8.8.8"]):
            pool.append(
                managed_network.add(
                    "fixture-" + uuid4().hex,
                    "http://fixture:" + marker + "@127.0.0.1:1",
                    ip,
                    "US",
                    "America/New_York",
                    "en-US",
                )["id"]
            )

        def allocate(aid):
            try:
                return managed_network.resolve(aid)
            except managed_network.NetworkWaiting:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(allocate, aids))
        assert sum(x is not None for x in results) == 2
        assert marker not in str(managed_network.inventory())
        with psycopg.connect(**database_kwargs(True)) as c:
            leases = c.execute(
                "SELECT agent_id,pool_id FROM lead.proxy_assignments WHERE pool_id=ANY(%s::uuid[]) AND released_at IS NULL",
                (pool,),
            ).fetchall()
            assert len({r[1] for r in leases}) == 2
            c.execute(
                "UPDATE lead.proxy_pool SET state='quarantined' WHERE id=%s",
                (leases[0][1],),
            )
        with pytest.raises(managed_network.NetworkWaiting):
            managed_network.resolve(str(leases[0][0]))
        from lead_app.browser import Actor

        with tx(str(leases[0][0])) as c:
            with pytest.raises(ValueError, match="出口已停用"):
                Actor(str(leases[0][0])).gate(c, {})
        # A quarantined assignment remains sticky; it is never borrowed from B.
        with tx(str(leases[0][0])) as c:
            assert (
                c.execute("SELECT lead.claim_proxy(%s)", (leases[0][0],)).fetchone()[
                    "claim_proxy"
                ]
                == "assigned"
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT * FROM lead.proxy_pool")
        domain.set_control(str(leases[1][0]), "archive")
        waiting = next(a for a, r in zip(aids, results) if r is None)
        assert managed_network.resolve(waiting)["password"] == marker
    finally:
        for aid in aids:
            domain.set_control(aid, "archive")
        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute(
                "DELETE FROM lead.proxy_assignments WHERE pool_id=ANY(%s::uuid[])",
                (pool,),
            )
            c.execute("DELETE FROM lead.proxy_pool WHERE id=ANY(%s::uuid[])", (pool,))
