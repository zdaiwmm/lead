"""A later read failure must not erase an unresolved send-permission barrier."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from lead_app import community_health as health


@pytest.fixture
def ledger(monkeypatch):
    rows = {}
    tenant = str(uuid4())

    def row(connection, aid, account, peer, url):
        return next((deepcopy(item) for item in rows.values()
                     if item["body"]["account"] == account and item["body"]["peer"] == peer), None)

    def save(connection, aid, key, body):
        rows[key] = {"id": key, "body": deepcopy(body)}

    # Keep failed(), recover(), observed(), and blocked() real; isolate ledger I/O.
    monkeypatch.setattr(health, "_row", row)
    monkeypatch.setattr(health, "_save", save)
    monkeypatch.setattr(health, "one", lambda *args: {"n": 0})
    monkeypatch.setattr(health, "current_tenant", lambda: tenant)
    return rows


def task(observation):
    return {"id": uuid4(), "input": {"external_account_id": "original-account"},
            "result": {"target_id": "original-peer", "reply_state": "unknown",
                       "original_receipt": "preserve", "history_observation": observation}}


ORIGINAL = {"joined": True, "can_reply": False, "verification_required": True,
            "membership_state": "restricted"}
REPAIRED = {"joined": True, "can_reply": True, "verification_required": False,
            "membership_state": "member"}


@pytest.mark.parametrize("read_fault", sorted(health.TRANSIENT))
def test_transient_read_preserves_send_barrier_and_original_evidence(ledger, monkeypatch, read_fault):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(health, "utcnow", lambda: now)
    original_task = task(ORIGINAL)
    failed = health.failed(None, "agent", original_task,
                           {"error_code": "send_failed", "stage": "reply", "retryable": True,
                            "retry_after": 36000}, occurred_at=now)
    read_task = task(REPAIRED)
    untouched = deepcopy(read_task)
    later = now + timedelta(minutes=1)
    event = {"error_code": read_fault, "stage": "receipt", "retryable": True,
             "scope": "account" if read_fault in {"browser_closed", "transport_error"} else "target"}
    state = health.failed(None, "agent", read_task, event, occurred_at=later)
    assert state["error_code"] == "send_failed"
    assert state["requires_changed_evidence"] is True
    assert state["failure_count"] == 2
    assert state["first_failure_at"] == failed["first_failure_at"]
    assert state["read_after"] == failed["read_after"]
    assert state["write_after"] == failed["write_after"]
    assert state["permission_evidence"] == ORIGINAL
    assert state["last_permission_evidence"] == ORIGINAL
    assert state["fault"]["error_code"] == read_fault
    assert health.failed(None, "agent", read_task, event, occurred_at=later) == state
    assert read_task == untouched
    assert health.blocked(None, "agent", read_task, write=True)["error_code"] == "send_failed"
    monkeypatch.setattr(health, "utcnow", lambda: now + timedelta(hours=11))
    health.observed(None, "agent", read_task, ORIGINAL)
    assert health.blocked(None, "agent", read_task, write=True)["error_code"] == "send_failed"
    health.observed(None, "agent", read_task, REPAIRED)
    assert health.blocked(None, "agent", read_task, write=True) is None


def test_read_failure_keeps_send_failure_backoff_and_first_permission_baseline(ledger, monkeypatch):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(health, "utcnow", lambda: now)
    first = task(ORIGINAL)
    health.failed(None, "agent", first, {"error_code": "send_failed", "stage": "reply"}, occurred_at=now)
    second = task(REPAIRED)
    later = now + timedelta(minutes=1)
    failed = health.failed(None, "agent", second,
                           {"error_code": "navigation_timeout", "stage": "observe"}, occurred_at=later)
    assert datetime.fromisoformat(failed["write_after"]) == later + timedelta(hours=2)
    assert datetime.fromisoformat(failed["read_after"]) == later + timedelta(hours=2)
    assert health.recover(failed, REPAIRED, later + timedelta(minutes=1))["state"] == "blocked"
    repeated = health.failed(None, "agent", task(REPAIRED),
                             {"error_code": "send_failed", "stage": "reply"},
                             occurred_at=later + timedelta(minutes=2))
    assert repeated["permission_evidence"] == ORIGINAL
    assert repeated["last_permission_evidence"] == ORIGINAL
    assert repeated["failure_count"] == 3


def test_a_recovered_send_failure_does_not_keep_an_obsolete_permission_baseline(ledger, monkeypatch):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(health, "utcnow", lambda: now)
    original = task(ORIGINAL)
    health.failed(None, "agent", original, {"error_code": "send_failed", "stage": "reply"}, occurred_at=now)
    monkeypatch.setattr(health, "utcnow", lambda: now + timedelta(hours=1))
    health.observed(None, "agent", original, REPAIRED)
    assert health.blocked(None, "agent", original) is None
    fresh = health.failed(None, "agent", task(REPAIRED), {"error_code": "send_failed", "stage": "reply"})
    assert fresh["permission_evidence"] == REPAIRED
    assert fresh["failure_count"] == 1


def test_permission_repaired_before_cooldown_recovers_when_same_repair_is_confirmed_later(ledger, monkeypatch):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(health, "utcnow", lambda: now)
    original = task(ORIGINAL)
    health.failed(None, "agent", original, {"error_code": "send_failed", "stage": "reply"}, occurred_at=now)
    health.failed(None, "agent", task(ORIGINAL),
                  {"error_code": "navigation_timeout", "stage": "observe"},
                  occurred_at=now + timedelta(minutes=1))
    monkeypatch.setattr(health, "utcnow", lambda: now + timedelta(minutes=10))
    health.observed(None, "agent", original, REPAIRED)
    blocked = health.blocked(None, "agent", original)
    assert blocked["permission_evidence"] == ORIGINAL
    assert blocked["last_permission_evidence"] == REPAIRED
    monkeypatch.setattr(health, "utcnow", lambda: now + timedelta(hours=3))
    health.observed(None, "agent", original, REPAIRED)
    assert health.blocked(None, "agent", original) is None
