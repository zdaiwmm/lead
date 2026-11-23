import pytest
from lead_app import chamber_analytics, distribution
from lead_app.db import tx


def test_chamber_registration_reuses_public_code_after_lost_receipt(monkeypatch, growth_agent):
    monkeypatch.setenv(chamber_analytics.LOCAL_AGENT, str(growth_agent))
    monkeypatch.setenv(chamber_analytics.ORIGIN, "https://analytics.example.com")
    attempts = []

    def remote(aid, code, platform, destination):
        attempts.append((str(aid), code, platform, destination))
        if len(attempts) == 1:
            raise chamber_analytics.ChamberAnalyticsUnavailable

    monkeypatch.setattr(chamber_analytics, "register_production_link", remote)
    destination = "https://example.com/"
    with pytest.raises(chamber_analytics.ChamberAnalyticsUnavailable), tx(growth_agent) as c:
        distribution.register(c, growth_agent, "telegram", destination, intent="post:1", placement="post")
    with tx(growth_agent) as c:
        row = distribution.register(
            c, growth_agent, "telegram", destination, intent="post:1", placement="post"
        )
    assert attempts == [(str(growth_agent), row["public_code"], "telegram", destination)] * 2


def test_existing_tracking_link_keeps_its_public_code_when_chamber_is_enabled(monkeypatch, growth_agent):
    kwargs = {"intent": "manual:existing", "placement": "post"}
    destination = "https://example.com/"
    with tx(growth_agent) as c:
        original = distribution.register(
            c, growth_agent, "github", destination, code="legacyChamber000", **kwargs
        )
    monkeypatch.setenv(chamber_analytics.LOCAL_AGENT, str(growth_agent))
    monkeypatch.setenv(chamber_analytics.ORIGIN, "https://analytics.example.com")
    observed = []
    monkeypatch.setattr(chamber_analytics, "register_production_link", lambda *args: observed.append(args))
    with tx(growth_agent) as c:
        candidate = distribution.registration_marker(
            growth_agent, "github", kwargs["intent"], kwargs["placement"], compact=False
        )
        reused = distribution.register(c, growth_agent, "github", destination, code=candidate, **kwargs)
    assert original == reused
    assert observed == [(growth_agent, original["public_code"], "github", destination)]
