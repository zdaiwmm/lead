"""Platform-specific evidence, common connection and operation protocol."""
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field

from .growth.contracts import Contract

BROWSER_FAILURE_CATEGORIES = frozenset(('browser_timeout', 'browser_error', 'timeout', 'key_error',
    'type_error', 'attribute_error', 'runtime_error', 'unexpected_error'))
BROWSER_FAILURE_MODULES = frozenset(('browser', 'community_runtime', 'telegram', 'identity',
    'account_connection', 'network_namespace', 'forward_proxy', 'community_search', 'db',
    'resources', 'account_profiles', 'managed_network', 'browser_runtime', 'telemetry'))
COMMUNITY_INSPECTION_ERRORS = frozenset(('community_discussion_binding_required',
    'community_source_binding_invalid', 'community_operation_identity_changed',
    'community_group_reference_invalid', 'community_public_group_required',
    'community_group_dependency_required'))


def browser_failure_metadata(error, root):
    """Code locations survive a failed RPC; exception/page/request text never does."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    name = type(error).__name__
    if type(error).__module__.startswith('playwright.'):
        category = 'browser_timeout' if name == 'TimeoutError' else 'browser_error'
    else:
        category = {TimeoutError: 'timeout', KeyError: 'key_error', TypeError: 'type_error',
                    AttributeError: 'attribute_error', RuntimeError: 'runtime_error'}.get(type(error), 'unexpected_error')
    value = {'category': category, 'module': 'unknown', 'line': None}
    frame = error.__traceback__
    while frame is not None:
        try:
            relative = Path(frame.tb_frame.f_code.co_filename).relative_to(Path(root) / 'services/api/lead_app')
            module = relative.with_suffix('').as_posix().replace('/', '.')
            if relative.suffix == '.py' and module in BROWSER_FAILURE_MODULES:
                value.update(module=module, line=frame.tb_lineno)
        except ValueError:
            pass
        frame = frame.tb_next
    return value


def read_browser_failure(payload):
    """Accept only fixed metadata from the registered executor, never its detail."""
    value = payload.get('browser_failure') if isinstance(payload, dict) else None
    if not isinstance(value, dict) or set(value) != {'category', 'module', 'line'}:
        return None
    category, module, line = value['category'], value['module'], value['line']
    if not isinstance(category, str) or category not in BROWSER_FAILURE_CATEGORIES or not isinstance(module, str):
        return None
    if module == 'unknown':
        return dict(value) if line is None else None
    if module in BROWSER_FAILURE_MODULES and type(line) is int and 0 < line < 1_000_000:
        return dict(value)
    return None


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
