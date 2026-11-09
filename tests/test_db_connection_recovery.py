"""Connection recovery cannot turn an uncertain transaction into a replay."""

import psycopg
import pytest
from lead_app import db


def test_transient_connect_failure_retries_before_any_transaction(monkeypatch):
    calls = []
    sleeps = []
    connected = object()

    def connect(**kwargs):
        calls.append(True)
        if len(calls) < 3:
            raise psycopg.OperationalError("fixture connection closed")
        return connected

    monkeypatch.setattr(db, "database_kwargs", lambda: {})
    monkeypatch.setattr(db.psycopg, "connect", connect)
    monkeypatch.setattr(db.time, "sleep", sleeps.append)
    assert db.connect_runtime() is connected
    assert len(calls) == 3 and sleeps == [0.1, 0.3]


@pytest.mark.parametrize("authentication_error", [False, True])
def test_connect_recovery_is_bounded_and_does_not_retry_credentials(
    monkeypatch, authentication_error
):
    calls = []
    sleeps = []

    def connect(**kwargs):
        calls.append(True)
        if authentication_error:
            raise psycopg.errors.InvalidPassword("fixture invalid credentials")
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db, "database_kwargs", lambda: {})
    monkeypatch.setattr(db.psycopg, "connect", connect)
    monkeypatch.setattr(db.time, "sleep", sleeps.append)
    with pytest.raises(psycopg.OperationalError):
        db.connect_runtime()
    assert len(calls) == (1 if authentication_error else 4)
    assert sleeps == ([] if authentication_error else [0.1, 0.3, 0.6])


@pytest.mark.parametrize("at_commit", [False, True])
def test_connection_or_commit_loss_after_transaction_starts_never_replays(
    monkeypatch, at_commit
):
    calls = []
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            if at_commit:
                raise psycopg.OperationalError("fixture lost commit acknowledgement")
            return False

        def execute(self, sql, params=()):
            statements.append(sql)
            if sql == "fixture committed write" and not at_commit:
                raise psycopg.OperationalError("fixture lost acknowledgement")

    def connect(**kwargs):
        calls.append(True)
        return Connection()

    monkeypatch.setattr(db, "database_kwargs", lambda: {})
    monkeypatch.setattr(db.psycopg, "connect", connect)
    with pytest.raises(psycopg.OperationalError):
        with db.tx(pooled=False) as c:
            c.execute("fixture committed write")
    assert len(calls) == 1 and statements.count("fixture committed write") == 1


def test_authentication_failure_without_sqlstate_is_not_retried(monkeypatch):
    def reject(**kwargs):
        raise psycopg.OperationalError("password authentication failed for fixture role")
    monkeypatch.setattr(db.psycopg, "connect", reject)
    monkeypatch.setattr(db, "database_kwargs", lambda: {})
    monkeypatch.setattr(db.time, "sleep", lambda _: pytest.fail("unexpected retry"))
    with pytest.raises(psycopg.OperationalError):
        db.connect_runtime()
