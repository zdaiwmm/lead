"""Platform-specific evidence, common connection and operation protocol."""
from typing import Literal
from uuid import UUID

from pydantic import Field

from .growth.contracts import Contract


class SubjectRef(Contract):
    namespace: str
    external_id: str
    id_kind: Literal["stable", "handle"]
    aliases: list[str] = Field(default_factory=list)


class SessionObservation(Contract):
    status: Literal["authenticated", "signed_out", "challenge", "ambiguous", "temporarily_unavailable"]
    operating_subject: SubjectRef | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    owner_action: str | None = None


class ExecutionRef(Contract):
    profile_id: UUID
    executor_ref: str
    transport_ref: str
    route_generation: int = Field(ge=1)
    registry_revision: str | None = Field(default=None, strict=True, pattern=r'^[0-9a-f]{64}$')


class TargetObservation(Contract):
    canonical_ref: str
    acting_subject: SubjectRef
    role: str
    operations: list[str]
    evidence_refs: list[str]


class SubmissionObservation(Contract):
    state: Literal["confirmed", "pending", "unknown", "not_applied"]
    external_id: str | None = None
    external_url: str | None = None
    visibility: Literal["public", "restricted", "pending", "unknown"] = "unknown"
    evidence_refs: list[str] = Field(default_factory=list)
