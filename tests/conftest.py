"""Test-only profile lifecycle; never garbage-collect user browser profiles."""

import fcntl
import shutil

import pytest
from lead_app import domain
from lead_app.config import STATE
from lead_app.db import one, tx


@pytest.fixture(autouse=True)
def reclaim_test_profiles(monkeypatch):
    original = domain.create_agent
    created = set()

    def tracked(*args, **kwargs):
        aid = original(*args, **kwargs)
        created.add(str(aid))
        return aid

    monkeypatch.setattr(domain, "create_agent", tracked)
    yield
    for aid in created:
        try:
            with tx(aid) as c:
                row = one(
                    c, "SELECT desired_state FROM lead.agents WHERE id=%s", (aid,)
                )
            if not row:
                continue
            if row["desired_state"] != "archived":
                domain.set_control(aid, "archive")
            folder = STATE / "profiles" / aid
            lock = folder / "actor.lock"
            if not lock.exists():
                continue
            with lock.open("a") as held:
                try:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                # This set consists only of IDs created through this test's spy.
                # Identity/evidence and DB histories are retained for inspection.
                storage = folder / "storage"
                if storage.is_dir() and not storage.is_symlink():
                    shutil.rmtree(storage)
        except (OSError, LookupError):
            pass
