"""Operator-configured ownership of legacy supervisor cycles, separate from channels."""

from uuid import UUID


def resident_cycle_agent_ids(values):
    """Validate every read so a dynamic configuration change cannot silently reroute work."""
    key = "LEAD_RESIDENT_CYCLE_AGENT_IDS"
    value = values.get(key)
    if value is None:
        return frozenset()
    if not isinstance(value, str):
        raise ValueError(key + " must contain comma-separated canonical UUIDs")
    if not value.strip():
        return frozenset()
    result = set()
    for item in value.split(","):
        item = item.strip()
        try:
            valid = str(UUID(item)) == item
        except ValueError:
            valid = False
        if not valid or item in result:
            raise ValueError(key + " must contain unique canonical UUIDs")
        result.add(item)
    return frozenset(result)
