from datetime import datetime, timedelta, timezone

import pytest
from lead_app.channel_calendar import (
    ChannelSchedule,
    decide,
    default_schedule,
    future_slots,
)
from pydantic import ValidationError

NOW = datetime(2026, 11, 2, 20, tzinfo=timezone.utc)  # Monday


def occupied(at, slot=None, state="succeeded"):
    return {"at": at, "slot": slot, "state": state}


def test_first_launch_then_spacing_and_next_day_slot_are_enforced():
    schedule = default_schedule("facebook")
    first = decide(schedule, [], NOW)
    assert first["due"] and first["slot"] == "launch:2026-11-02"
    assert (
        decide(schedule, [occupied(NOW, first["slot"])], NOW + timedelta(hours=1))[
            "reason"
        ]
        == "channel_spacing"
    )
    assert not decide(
        schedule, [occupied(NOW, first["slot"])], NOW + timedelta(days=1)
    )["due"]
    wed = NOW + timedelta(days=2)
    assert decide(schedule, [occupied(NOW, first["slot"])], wed)["due"]


def test_weekly_limits_unknown_results_and_reserved_slot_prevent_bursts():
    schedule = default_schedule("facebook")
    reservations = [
        occupied(NOW - timedelta(days=d), state="reconciling") for d in (1, 3, 5)
    ]
    assert decide(schedule, reservations, NOW)["reason"] == "weekly_limit"
    assert not decide(schedule, [occupied(NOW, "launch:2026-11-02", "queued")], NOW)[
        "due"
    ]
    assert decide(schedule, [occupied(NOW, state="failed")], NOW)["due"]


def test_only_last_day_is_caught_up_and_timezone_is_respected():
    schedule = {**default_schedule("facebook", "Asia/Shanghai"), "hour": 19}
    next_at = future_slots(schedule, datetime(2026, 11, 2, 10, tzinfo=timezone.utc), 1)[
        0
    ]
    assert next_at.hour == 11
    old = occupied(NOW - timedelta(days=20))
    # Sunday, no Friday backlog is emitted as a burst.
    assert not decide(schedule, [old], datetime(2026, 11, 8, 20, tzinfo=timezone.utc))[
        "due"
    ]
    assert default_schedule("youtube")["max_per_7_days"] == 1
    assert default_schedule("pinterest")["max_per_7_days"] == 6
    with pytest.raises(ValidationError):
        ChannelSchedule(timezone="unknown/timezone")
    with pytest.raises(ValidationError):
        ChannelSchedule(weekdays=[0, 0])
