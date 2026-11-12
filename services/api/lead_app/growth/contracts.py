"""Validated protocol shared by the decision maker, runner and tools."""
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Kind = Literal["decision", "research", "content", "account", "channel", "observe", "recover"]
Question = Literal["product", "audience", "distribution", "value", "conversion", "retention"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(Contract):
    tenant_id: UUID
    agent_id: UUID


class TaskRef(Contract):
    agent_id: UUID
    task_id: UUID
    task_generation: int = Field(default=1, ge=1)


class EvidenceRef(Contract):
    kind: Literal["business", "task", "document", "action", "event"]
    id: UUID
    revision: int | None = Field(default=None, ge=1)
    claim: str = Field(default="", max_length=1000)


class GoalRevision(Contract):
    revision: int = Field(ge=1)


class GoalRef(Contract):
    revision: int = Field(default=1, ge=1)
    connection_id: UUID | None = None
    event_name: str | None = Field(default=None, max_length=120)
    definition_revision: int | None = Field(default=None, ge=1)
    unit: Literal["user", "pair", "order", "organization"] = "user"
    newness_rule: Literal["trusted_first_value"] = "trusted_first_value"
    quality_predicate: Literal["versioned_core_value"] = "versioned_core_value"
    trusted_producer: Literal["server_verified"] = "server_verified"
    identity_scope: Literal["connection"] = "connection"
    exclusions: list[Literal["team", "partner_acceptance", "diagnostic"]] = Field(
        default_factory=lambda: ["team", "partner_acceptance", "diagnostic"], max_length=3)
    attribution_window_days: int = Field(default=30, ge=1, le=90)
    delay_allowance_hours: int = Field(default=72, ge=0, le=720)
    freshness_hours: int = Field(default=24, ge=1, le=720)


class ExperimentSpec(Contract):
    question_type: Question
    question: str = Field(min_length=3, max_length=500)
    hypothesis: str = Field(min_length=3, max_length=1200)
    learning_signal: str = Field(min_length=3, max_length=500)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    token_limit: int = Field(gt=0, le=10_000_000)
    review_at: AwareDatetime
    stop_rule: str = Field(default="Stop when the observation window or working budget ends.", min_length=3, max_length=500)
    audience: str = Field(default="", max_length=500)
    variation: str = Field(default="initial", max_length=120)
    parent_experiment_id: UUID | None = None
    activation_ref: GoalRef | None = None


class Wait(Contract):
    reason: str = Field(min_length=1, max_length=120)
    producer: str = Field(min_length=1, max_length=120)
    check_at: AwareDatetime
    recovery_action: Literal["recheck", "probe", "reconcile", "owner", "configuration", "decision"]
    episode_root: UUID
    responsibility: Literal["owner", "product", "platform", "resource"] = "product"
    resume_on: list[str] = Field(default_factory=list, max_length=8)
    scope_ref: str = Field(default="project", max_length=200)
    escalation: str = Field(default="Report the unresolved condition; continue independent work.", min_length=3, max_length=1000)


class RequiredRef(Contract):
    task_id: UUID
    artifact: str = Field(default="output", min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=500)


class TaskSpec(Contract):
    kind: Kind
    purpose: str = Field(min_length=1, max_length=120)
    experiment_id: UUID | None = None
    account_id: UUID | None = None
    target_id: UUID | None = None
    required_refs: list[RequiredRef] = Field(default_factory=list, max_length=16)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    payload: dict[str, Any] = Field(default_factory=dict)
    completion_rule: str = Field(default="Return a source-backed result.", min_length=3, max_length=500)
    review_at: AwareDatetime
    priority: Literal["control", "normal", "background"] = "normal"
    dedupe_key: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def no_privilege_payload(self):
        forbidden = {"approved", "desired", "technical_verified", "automation_enabled", "transport_ref"}
        if forbidden.intersection(self.payload):
            raise ValueError("Tool payload cannot grant authority")
        if self.kind == "channel" and (not self.account_id or not self.target_id):
            raise ValueError("Channel work requires an exact account and target")
        return self


class Command(Contract):
    command_key: str = Field(min_length=1, max_length=120)
    group_key: str = Field(min_length=1, max_length=120)
    kind: Literal["experiment.create", "experiment.revise", "experiment.close", "task.create", "target.propose"]
    experiment_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    account_id: UUID | None = None
    expected_binding: int | None = Field(default=None, ge=1)
    goal_ref: GoalRevision
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    payload: dict[str, Any]


class DecisionProposal(Contract):
    context_key: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=3, max_length=3000)
    commands: list[Command] = Field(max_length=12)


class WorkResult(Contract):
    outcome: Literal["completed", "waiting", "failed", "unknown"]
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    wait: Wait | None = None
    checkpoint: dict[str, Any] | None = None
    costs: dict[str, int] = Field(default_factory=dict)
    failure_code: str | None = None

    @model_validator(mode="after")
    def waiting_has_producer(self):
        if self.outcome == "waiting" and self.wait is None:
            raise ValueError("Waiting requires a durable condition and producer")
        return self


def utcnow() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc)
