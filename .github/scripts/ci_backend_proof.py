"""Verify same-attempt backend coverage from sanitized pytest shard reports."""

import argparse
import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

SHARD_COUNT = 4
EARLY_NODEIDS = (
    "tests/test_native_private_cache_product.py::test_private_identity_probe_bypasses_stale_worker_cache_without_disabling_business_worker",
    "tests/test_native_same_account_parallel_product.py::test_unknown_receipt_read_allows_same_account_independent_original_task",
    "tests/test_research_recovery_integration.py::test_original_source_recovery_through_real_outbox_temporal_and_current_dependencies[both-current]",
    "tests/test_research_recovery_integration.py::test_original_source_recovery_through_real_outbox_temporal_and_current_dependencies[one-expired]",
)
EARLY_FILES = {node.split("::", 1)[0] for node in EARLY_NODEIDS}
PHASES = ("setup", "call", "teardown")


def node_hash(nodeid):
    return hashlib.sha256(nodeid.encode()).hexdigest()


def owner(file):
    return 0 if file in EARLY_FILES else int(node_hash(file), 16) % SHARD_COUNT


def require(condition, message):
    if not condition:
        raise ValueError(message)


def duration(value):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            "Invalid timing")
    return value


def collection_entries(report):
    entries = report["collection"]
    require(isinstance(entries, list) and entries, "Empty collection")
    for item in entries:
        require(set(item) == {"id", "file", "name"}, "Invalid collection entry")
        require(re.fullmatch(r"[0-9a-f]{64}", item["id"]) is not None, "Invalid test identity")
        file = item["file"]
        require(re.fullmatch(r"[A-Za-z0-9_./-]+\.py", file) is not None
                and not file.startswith("/") and ".." not in file.split("/"), "Invalid test file")
        require(re.fullmatch(r"[A-Za-z0-9_:.-]+", item["name"]) is not None, "Invalid test name")
    ids = [item["id"] for item in entries]
    require(len(ids) == len(set(ids)), "Duplicate collected test")
    return entries


def validate_collection(reports):
    """Audit partition coverage; this alone does not prove test execution."""
    require(len(reports) == SHARD_COUNT, "Missing or additional suite report")
    require(Counter(r["shard"] for r in reports) == Counter(range(SHARD_COUNT)),
            "Missing or duplicate shard")
    original = collection_entries(reports[0])
    selected = []
    for report in reports:
        require(report["stage"] == "suite" and report["shard_count"] == SHARD_COUNT,
                "Invalid suite shard")
        require(collection_entries(report) == original, "Shard collections disagree")
        expected = [item["id"] for item in original if owner(item["file"]) == report["shard"]]
        require(report["selected"] == expected and expected, "Shard selection differs from ownership")
        selected.extend(expected)
    require(Counter(selected) == Counter(item["id"] for item in original),
            "Missing or duplicate coverage")
    return original


def verify(reports, identity):
    require(len(reports) == SHARD_COUNT + 1, "Expected four suite reports and one early report")
    require(set(identity) == {"sha", "run_id", "run_attempt"}
            and re.fullmatch(r"[0-9a-f]{40}", identity["sha"]) is not None
            and all(re.fullmatch(r"[1-9][0-9]*", identity[key]) for key in ("run_id", "run_attempt")),
            "Exact CI identity required")
    for report in reports:
        require(report["schema"] == 1 and report["identity"] == identity, "Report identity mismatch")
        require(type(report["shard"]) is int and 0 <= report["shard"] < SHARD_COUNT
                and report["shard_count"] == SHARD_COUNT, "Invalid shard identity")
        require(type(report["exitstatus"]) is int and report["exitstatus"] == 0
                and report["collect_only"] is False and report["report_errors"] == 0,
                "Incomplete or unsuccessful pytest session")
        duration(report["session_seconds"])
    suite = [r for r in reports if r["stage"] == "suite"]
    original = validate_collection(suite)
    early = [r for r in reports if r["stage"] == "early"]
    require(len(early) == 1 and early[0]["shard"] == 0, "Missing or duplicate early regression")
    early_ids = [node_hash(node) for node in EARLY_NODEIDS]
    require([item["id"] for item in collection_entries(early[0])] == early_ids
            and early[0]["selected"] == early_ids, "Early regression coverage differs")
    early_items = {item["id"]: item for item in early[0]["collection"]}
    require(early_items == {item["id"]: item for item in original if item["id"] in early_ids}
            and all(owner(item["file"]) == 0 for item in early_items.values()),
            "Early tests absent or different in full suite")
    totals = Counter()
    outcomes = Counter()
    subtests = Counter()
    slowest = []
    for report in reports:
        require(set(report["cases"]) == set(report["selected"]), "Missing or unexpected execution")
        counts = report["subtests"]
        require(set(counts) == {"passed", "failed", "skipped"}
                and all(type(n) is int and n >= 0 for n in counts.values())
                and counts["failed"] == 0, "Invalid or failed subtests")
        if report["stage"] == "suite":
            subtests.update(counts)
        lookup = {item["id"]: item for item in report["collection"]}
        for test_id, phases in report["cases"].items():
            require(set(phases) in ({"setup", "call", "teardown"}, {"setup", "teardown"}),
                    "Incomplete test phases")
            for phase, result in phases.items():
                require(set(result) == {"duration", "outcome"}
                        and result["outcome"] in {"passed", "skipped"}, "Failed or malformed test phase")
                duration(result["duration"])
            require(phases["teardown"]["outcome"] == "passed", "Teardown did not pass")
            setup_skipped = phases["setup"]["outcome"] == "skipped"
            require(("call" not in phases) == setup_skipped, "Call phase does not match setup")
            if test_id in early_ids:
                require(set(phases) == set(PHASES)
                        and all(result["outcome"] == "passed" for result in phases.values()),
                        "Early regression must pass in both runs")
            if report["stage"] == "suite":
                for phase, result in phases.items():
                    totals[phase] += result["duration"]
                outcome = "skipped" if setup_skipped else phases["call"]["outcome"]
                outcomes[outcome] += 1
                item = lookup[test_id]
                slowest.append({"id": test_id, "file": item["file"], "name": item["name"],
                                "seconds": sum(p["duration"] for p in phases.values())})
    return {"identity": identity, "tests": len(original), "shards": SHARD_COUNT,
            "early_repeated_tests": len(early_ids), "outcomes": dict(outcomes),
            "subtests": dict(subtests), "phase_seconds": dict(totals),
            "slowest": sorted(slowest, key=lambda item: item["seconds"], reverse=True)[:15]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    args = parser.parse_args()
    paths = sorted(args.reports.rglob("*.json"))
    identity = {"sha": os.environ["GITHUB_SHA"], "run_id": os.environ["GITHUB_RUN_ID"],
                "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"]}
    print(json.dumps(verify([json.loads(path.read_text()) for path in paths], identity), indent=2))


if __name__ == "__main__":
    main()
