import pytest
from lead_app import budget, domain
from lead_app.db import one, tx, uid


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(budget, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(budget, "env", lambda: {})
    return domain.create_agent()


def test_default_is_one_billion_and_changes_apply_to_today_without_resetting_usage(
    agent,
):
    assert budget.get_configuration(agent)["daily_token_limit"] == 1_000_000_000
    reservation = budget.reserve(agent, uid(), 120)
    budget.settle(agent, reservation, actual=100)
    pending = budget.reserve(agent, uid(), 30)
    budget.settle(agent, pending)  # uncertain usage stays reserved
    budget.set_daily_limit(agent, 125)
    with tx(agent) as c:
        assert one(
            c, "SELECT limit_units,spent_units,reserved_units FROM lead.budget_periods"
        ) == {"limit_units": 125, "spent_units": 100, "reserved_units": 30}
    with pytest.raises(budget.BudgetExceeded):
        budget.reserve(agent, uid(), 1)
    budget.set_daily_limit(agent, 1_000_000_000)
    assert budget.reserve(agent, uid(), 1)


def test_custom_limit_and_restore_default_are_scoped(agent, monkeypatch):
    other = domain.create_agent()
    budget.set_daily_limit(agent, 123456)
    monkeypatch.setattr(budget, "env", lambda: {"LEAD_DAILY_TOKEN_LIMIT": "2000000000"})
    assert budget.get_configuration(agent)["daily_token_limit"] == 123456
    assert budget.get_configuration(other)["daily_token_limit"] == 2_000_000_000
    config = budget.set_daily_limit(agent, None)
    assert config["daily_token_limit"] == 2_000_000_000
    assert config["custom_daily_token_limit"] is None


def test_zero_blocks_new_calls_and_invalid_values_do_not_modify_limit(agent):
    budget.set_daily_limit(agent, 0)
    with pytest.raises(budget.BudgetExceeded):
        budget.reserve(agent, uid(), 1)
    for value in (-1, True, 1.5, "1000", budget.MAX_DAILY_TOKEN_LIMIT + 1):
        with pytest.raises(ValueError):
            budget.set_daily_limit(agent, value)
    assert budget.get_configuration(agent)["daily_token_limit"] == 0


def test_partial_provider_usage_records_known_tokens_and_keeps_unknown_reserved(agent):
    reservation = budget.reserve(agent, uid(), 100)
    budget.settle(
        agent,
        reservation,
        actual=20,
        incomplete=True,
        details={"unmeasured_requests": 1},
    )
    budget.settle(agent, reservation, actual=20, incomplete=True)
    with tx(agent) as c:
        assert one(c, "SELECT reserved_units,spent_units FROM lead.budget_periods") == {
            "reserved_units": 100,
            "spent_units": 20,
        }
        assert (
            one(c, "SELECT state FROM lead.budget_reservations")["state"] == "unknown"
        )
        assert one(c, "SELECT count(*) AS n FROM lead.usage_events")["n"] == 1
