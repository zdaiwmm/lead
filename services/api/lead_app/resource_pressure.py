"""Cheap Linux pressure samples; unsupported hosts retain the existing policy."""

import time
from pathlib import Path

import psutil

from .config import env

_cached = (0, {})
_loop_lag = (0, 0)


def record_loop_lag(seconds):
    global _loop_lag
    _loop_lag = (time.monotonic(), seconds)


def bounded_int(name, default, minimum, maximum):
    raw = env().get(name)
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def psi(resource):
    try:
        lines = Path("/proc/pressure/" + resource).read_text().splitlines()
        return {parts[0]: float(dict(pair.split("=") for pair in parts[1:])["avg10"])
                for line in lines if (parts := line.split())}
    except (OSError, ValueError, KeyError):
        return {}


def sample():
    global _cached
    clock = time.monotonic()
    if clock - _cached[0] < 5:
        return _cached[1]
    memory = psutil.virtual_memory()
    values = {"memory_available_mb": memory.available // (1024 * 1024),
              "memory_full_psi": psi("memory").get("full", 0),
              "cpu_some_psi": psi("cpu").get("some", 0),
              "event_loop_lag_seconds": _loop_lag[1] if clock-_loop_lag[0]<60 else 0}
    try:
        values["cgroup_memory_events"] = {k: int(v) for k, v in (line.split() for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines())}
    except (OSError, ValueError):
        values["cgroup_memory_events"] = {}
    old_events = _cached[1].get("cgroup_memory_events", {})
    values["new_oom"] = any(values["cgroup_memory_events"].get(k, 0) > old_events.get(k, 0) for k in ("oom", "oom_kill")) if old_events else False
    values["memory_pressure"] = values["memory_full_psi"] >= 5 or values["memory_available_mb"] < bounded_int("LEAD_MIN_AVAILABLE_MB", 512, 128, 8192) or values["new_oom"]
    values["defer_optional"] = values["memory_pressure"] or values["cpu_some_psi"] >= 50 or values["event_loop_lag_seconds"] >= 2
    _cached = (clock, values)
    return values


def browser_idle_seconds():
    return bounded_int("LEAD_BROWSER_IDLE_SECONDS", 300, 30, 3600)
