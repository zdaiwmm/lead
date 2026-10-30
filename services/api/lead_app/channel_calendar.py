"""Deterministic channel cadence shared by planning and the final queue gate."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import all_rows


class ChannelSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str = "UTC"
    weekdays: list[int] = Field(
        default_factory=lambda: [0, 2, 4], min_length=1, max_length=7
    )
    hour: int = Field(default=19, ge=0, le=23)
    max_per_7_days: int = Field(default=3, ge=1, le=14)
    min_gap_hours: int = Field(default=20, ge=1, le=168)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("运营时区无效") from None
        return value

    @field_validator("weekdays")
    @classmethod
    def valid_days(cls, value):
        if len(set(value)) != len(value) or any(
            type(d) is not int or not 0 <= d <= 6 for d in value
        ):
            raise ValueError("运营星期需要是不重复的 0–6")
        return sorted(value)


def default_schedule(platform, zone="UTC"):
    values = {"timezone": zone}
    if platform in {"x", "pinterest", "threads"}:
        values.update(weekdays=[0, 1, 2, 3, 4, 5], max_per_7_days=6)
    elif platform == "youtube":
        values.update(weekdays=[2], max_per_7_days=1, min_gap_hours=144)
    return ChannelSchedule(**values).model_dump()


def future_slots(schedule, now=None, count=8):
    schedule = ChannelSchedule.model_validate(schedule)
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(schedule.timezone))
    slots = []
    for days in range(70):
        day = local.date() + timedelta(days=days)
        if day.weekday() not in schedule.weekdays:
            continue
        when = datetime(
            day.year,
            day.month,
            day.day,
            schedule.hour,
            tzinfo=ZoneInfo(schedule.timezone),
        )
        if when > now:
            slots.append(when.astimezone(timezone.utc))
        if len(slots) == count:
            break
    return slots


def decide(schedule, reservations, now=None):
    """Catch up at most one missed slot; never burst to fill missed weekdays."""
    config = ChannelSchedule.model_validate(schedule)
    now = now or datetime.now(timezone.utc)
    occupied = [r for r in reservations if r["state"] not in {"failed", "canceled"}]
    recent = [r for r in occupied if r["at"] > now - timedelta(days=7)]
    upcoming = future_slots(config.model_dump(), now, 1)[0]
    latest = max((r["at"] for r in occupied), default=None)
    if len(recent) >= config.max_per_7_days:
        due = min(r["at"] for r in recent) + timedelta(days=7, seconds=1)
        return {
            "due": False,
            "reason": "weekly_limit",
            "next_at": due.isoformat(),
            "delay": max(1, math_seconds(due - now)),
        }
    if latest and now < latest + timedelta(hours=config.min_gap_hours):
        due = latest + timedelta(hours=config.min_gap_hours)
        return {
            "due": False,
            "reason": "channel_spacing",
            "next_at": due.isoformat(),
            "delay": max(1, math_seconds(due - now)),
        }
    if not occupied:
        slot = "launch:" + now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
    else:
        candidates = future_slots(config.model_dump(), now - timedelta(hours=24), 2)
        previous = [p for p in candidates if p <= now]
        if not previous:
            return {
                "due": False,
                "reason": "channel_calendar",
                "next_at": upcoming.isoformat(),
                "delay": max(1, math_seconds(upcoming - now)),
            }
        due = previous[-1]
        slot = due.isoformat()
        if any(r.get("slot") == slot or r["at"] >= due for r in occupied):
            return {
                "due": False,
                "reason": "slot_reserved",
                "next_at": upcoming.isoformat(),
                "delay": max(1, math_seconds(upcoming - now)),
            }
    return {
        "due": True,
        "slot": slot,
        "timezone": config.timezone,
        "schedule": config.model_dump(),
        "next_at": upcoming.isoformat(),
        "delay": 0,
    }


def math_seconds(delta):
    return int(delta.total_seconds()) + 1


def gate(c, target_id, schedule, now=None, exclude_task_id=None):
    rows = all_rows(
        c,
        """SELECT t.state,coalesce(a.confirmed_at,t.scheduled_at) AS at,t.input->'calendar'->>'slot' AS slot
           FROM lead.tasks t LEFT JOIN lead.actions a ON a.task_id=t.id
           WHERE t.input->>'target_id'=%s AND t.type LIKE '%%.publish'
             AND (%s::uuid IS NULL OR t.id<>%s::uuid)
             AND t.scheduled_at>now()-interval '30 days'
             AND coalesce(t.input->>'bootstrap_verification','false')<>'true'
             AND (t.state IN ('queued','running','reconciling','succeeded') OR a.state IN ('dispatching','unknown','confirmed'))""",
        (str(target_id), exclude_task_id, exclude_task_id),
    )
    # Failed tasks whose action remains uncertain still reserve capacity.
    for row in rows:
        if row["state"] in {"failed", "canceled"}:
            row["state"] = "reconciling"
    return decide(schedule, rows, now)
