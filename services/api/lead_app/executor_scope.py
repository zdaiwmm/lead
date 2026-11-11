"""Immutable profile ownership inherited by a channel executor's async tasks."""

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

_binding = ContextVar("lead_executor_profile", default=None)


@contextmanager
def profile(agent_uuid, profile_uuid):
    agent, selected = str(UUID(str(agent_uuid))), str(UUID(str(profile_uuid)))
    # Nested work may restate its binding, but cannot escape its parent executor.
    selected_profile(agent, selected)
    token = _binding.set((agent, selected))
    try:
        yield
    finally:
        _binding.reset(token)


def selected_profile(aid, explicit=None):
    binding = _binding.get()
    if binding is None:
        return explicit
    agent, selected = binding
    if str(UUID(str(aid))) != agent:
        raise ValueError("executor agent does not match the bound agent")
    if explicit is not None and str(UUID(str(explicit))) != selected:
        raise ValueError("executor profile does not match the bound profile")
    return selected
