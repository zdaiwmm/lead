from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 11, 11, 9, tzinfo=timezone.utc)


def evidence(**changes):
    from lead_app.execution_cadence import Scope, WaitEvidence

    return WaitEvidence(**{**dict(scope=Scope(agent_id="a", account_id="x", binding_version=2),
                                 source="engineering", reason_code="temporary_failure", evidence_ref="config:runtime@1",
                                 observed_at=NOW, policy_version=1), **changes})


def decision(items, **kwargs):
    from lead_app.execution_cadence import Scope, decide

    return decide(items, NOW, subject=Scope(agent_id="a", account_id="x", binding_version=2, target_id="group", operation="reply"), **kwargs)


def test_max_deadline_keeps_all_constraints():
    result = decision([evidence(not_before=NOW+timedelta(minutes=3)), evidence(not_before=NOW+timedelta(minutes=8))])
    assert result.state == "waiting_time"
    assert result.next_at == NOW+timedelta(minutes=8)
    assert len(result.constraints) == 2


def test_event_wait_survives_expired_time():
    result = decision([evidence(not_before=NOW-timedelta(seconds=1)), evidence(source="group_rule", resume_event="rules_changed")])
    assert result.state == "waiting_event"
    assert result.next_at is None
    assert len(result.constraints) == 2
    assert result.execution_cadence_protocol == 1


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError):
        evidence(not_before=datetime(2026, 11, 11))
    with pytest.raises(ValueError):
        evidence(observed_at=datetime(2026, 11, 11))


def test_expired_rule_evidence_requires_revalidation():
    result = decision([evidence(source="group_rule", valid_until=NOW-timedelta(seconds=1))])
    assert result.state == "waiting_event"
    assert "evidence_refresh" in result.resume_events


def test_uncertain_write_is_receipt_only():
    assert decision([evidence(resume_event="login_changed")], uncertain_write=True).state == "receipt_only"


def test_unrelated_target_is_not_blocked():
    from lead_app.execution_cadence import Scope

    assert decision([evidence(scope=Scope(agent_id="a", account_id="x", target_id="other"), not_before=NOW+timedelta(days=1))]).state == "ready"


def test_account_feedback_applies_to_each_target_but_not_old_binding():
    from lead_app.execution_cadence import Scope

    assert decision([evidence(not_before=NOW+timedelta(seconds=4))]).state == "waiting_time"
    assert decision([evidence(scope=Scope(agent_id="a", account_id="x", binding_version=1), not_before=NOW+timedelta(days=1))]).state == "ready"


def test_raw_text_is_not_platform_rate_evidence():
    from lead_app.community_health import fault

    result = fault(ValueError("Some page said FLOOD_WAIT_900 and 429"), "observe")
    assert result["error_code"] != "rate_limited"
    assert result.get("source") != "platform"
