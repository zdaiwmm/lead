"""Deterministic scope-bound waiting decisions; never grants authority."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: str = Field(min_length=1)
    account_id: str | None = None
    binding_version: int | None = Field(default=None, ge=1)
    target_id: str | None = None
    operation: str | None = None

    def applies_to(self, subject: "Scope") -> bool:
        return all(value is None or getattr(subject, key) is None or value == getattr(subject, key)
                   for key, value in self.model_dump().items())


class WaitEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Scope
    source: Literal["platform", "group_rule", "owner", "engineering", "receipt", "capability"]
    reason_code: str = Field(min_length=1)
    evidence_ref: str = Field(min_length=1)
    observed_at: datetime
    valid_until: datetime | None = None
    not_before: datetime | None = None
    resume_event: str | None = Field(default=None, min_length=1)
    policy_version: int = Field(ge=1)

    @field_validator("observed_at", "valid_until", "not_before")
    @classmethod
    def aware_time(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Cadence timestamps require a timezone")
        return value


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal["ready", "waiting_time", "waiting_event", "receipt_only", "terminal"]
    constraints: tuple[WaitEvidence, ...] = ()
    next_at: datetime | None = None
    resume_events: tuple[str, ...] = ()
    execution_cadence_protocol: Literal[1] = 1

    @field_validator("next_at")
    @classmethod
    def aware_deadline(cls, value):
        return WaitEvidence.aware_time(value)


def decide(evidence: list[WaitEvidence], now: datetime, *, subject: Scope,
           uncertain_write: bool = False) -> Decision:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Decision time requires a timezone")
    applicable = tuple(item for item in evidence if item.scope.applies_to(subject))
    if uncertain_write:
        return Decision(state="receipt_only", constraints=applicable)
    if any(item.reason_code == "duplicate_operation" for item in applicable):
        return Decision(state="terminal", constraints=applicable)
    events = {item.resume_event for item in applicable if item.resume_event}
    if any(item.valid_until is not None and item.valid_until <= now for item in applicable):
        events.add("evidence_refresh")
    if events:
        return Decision(state="waiting_event", constraints=applicable, resume_events=tuple(sorted(events)))
    future = [item.not_before for item in applicable if item.not_before and item.not_before > now]
    if future:
        return Decision(state="waiting_time", constraints=applicable, next_at=max(future))
    return Decision(state="ready", constraints=applicable)
