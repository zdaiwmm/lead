"""Resource-aware shadow planning. Only admitted candidates enter the planner."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkResources:
    slots: frozenset[str]
    exclusive: bool = False


def resources(choice):
    kind = choice.kind
    if kind == "kits":
        return WorkResources(frozenset({"channel_drafts"}))
    if kind == "operating.strategy":
        return WorkResources(frozenset({"model", "strategy"}))
    if kind in {"operating.journey", "operating.validate", "operating.opportunities"}:
        return WorkResources(frozenset({"public_browser", "model"}))
    if kind == "content":
        return WorkResources(frozenset({"model", "render", "content_inventory"}))
    if kind == "audience" or kind == "community_progress":
        slots = {"login_session"}
        if kind == "community_progress" and (choice.community_operation or {}).get(
            "paid", True
        ):
            slots.add("model")
        return WorkResources(frozenset(slots))
    if kind == "community_discovery":
        # Discovery can advance to native search in the same call. Retain the
        # account resource until a phase-specific handler enforces a narrower scope.
        return WorkResources(frozenset({"public_browser", "login_session", "model"}))
    # Compound publication/account paths can reach a device-specific adapter.
    return WorkResources(frozenset(), exclusive=True)


def shadow_batch(choices, capacity=2):
    """Preserve business priority; skip a conflicting head-of-line candidate."""
    selected = []
    occupied = set()
    seen = set()
    for choice in choices:
        if len(selected) >= capacity:
            break
        work = resources(choice)
        if choice.key in seen or occupied.intersection(work.slots):
            continue
        if work.exclusive and selected:
            continue
        selected.append(
            {
                "key": choice.key,
                "resources": sorted(work.slots),
                "exclusive": work.exclusive,
            }
        )
        seen.add(choice.key)
        occupied.update(work.slots)
        if work.exclusive:
            break
    return selected
