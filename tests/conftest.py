"""Test-only profile lifecycle; never garbage-collect user browser profiles."""

import fcntl
import os
import shutil

import pytest
from lead_app import domain
from lead_app.config import STATE
from lead_app.db import one, tx


@pytest.fixture(autouse=True)
def verification_scope():
    from lead_app.scope import tenant_scope

    if tenant := os.environ.get("LEAD_VERIFICATION_TENANT"):
        with tenant_scope(tenant):
            yield
    else:
        yield


@pytest.fixture(autouse=True)
def reclaim_test_profiles(monkeypatch, verification_scope):
    original = domain.create_agent
    created = set()

    def tracked(*args, **kwargs):
        # Existing regressions exercise the retained legacy contracts explicitly.
        # New public API calls pass growth; new growth fixtures also choose it explicitly.
        kwargs.setdefault("runtime_engine","legacy")
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


@pytest.fixture
def local_model_settings(monkeypatch):
    for provider in ("OPENAI", "DEEPSEEK"):
        monkeypatch.setenv(provider + "_API_KEY", "fixture-only")
        monkeypatch.setenv(provider + "_BASE_URL", "http://127.0.0.1:1")


@pytest.fixture
def process_pending_clicks():
    from lead_app.analytics_ingest import process_ingest, scoped_tx, work_args
    from lead_app.scope import current_tenant

    def process(aid):
        with scoped_tx(current_tenant(), aid, "live") as conn:
            pending = conn.execute(
                "SELECT * FROM lead.analytics_ingest_events WHERE state='pending'"
            ).fetchall()
        assert pending
        for item in pending:
            assert process_ingest(work_args(item))["state"] == "processed"

    return process


@pytest.fixture
def growth_agent():
    """A disposable project in the explicitly selected verification database."""
    aid = domain.create_agent(runtime_engine="growth")
    yield aid
    domain.set_control(aid, "archive")
