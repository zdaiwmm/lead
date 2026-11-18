import json
import os
import re
import time
from pathlib import Path

import pytest
from ci_backend_proof import SHARD_COUNT, node_hash, owner


def pytest_addoption(parser):
    group = parser.getgroup("lead-ci")
    group.addoption("--ci-shard", type=int, choices=range(SHARD_COUNT))
    group.addoption("--ci-stage", choices=("early", "suite"), default="suite")
    group.addoption("--ci-report")


def pytest_configure(config):
    shard = config.getoption("ci_shard")
    path = config.getoption("ci_report")
    if (shard is None) != (path is None):
        raise pytest.UsageError("--ci-shard and --ci-report must be used together")
    if shard is not None:
        config.pluginmanager.register(ShardReport(config, shard, Path(path)), "lead-ci-report")


class ShardReport:
    def __init__(self, config, shard, path):
        self.config, self.shard, self.path = config, shard, path
        self.started = time.monotonic()
        self.report = {
            "schema": 1,
            "identity": {"sha": os.environ.get("GITHUB_SHA", ""),
                         "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                         "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")},
            "shard": shard, "shard_count": SHARD_COUNT,
            "stage": config.getoption("ci_stage"), "collect_only": config.option.collectonly,
            "collection": [], "selected": [], "cases": {}, "report_errors": 0,
            "subtests": {"passed": 0, "failed": 0, "skipped": 0},
        }

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, config, items):
        self.report["collection"] = [
            {"id": node_hash(item.nodeid), "file": item.nodeid.split("::", 1)[0],
             "name": re.sub(r"[^A-Za-z0-9_:.-]", "_", item.nodeid.split("::", 1)[-1].split("[", 1)[0])}
            for item in items
        ]
        result = yield
        selected, removed = [], []
        for item in items:
            (selected if owner(item.nodeid.split("::", 1)[0]) == self.shard else removed).append(item)
        items[:] = selected
        self.report["selected"] = [node_hash(item.nodeid) for item in selected]
        if removed:
            config.hook.pytest_deselected(items=removed)
        return result

    def pytest_runtest_logreport(self, report):
        # Native pytest subtests emit additional call reports for the parent ID.
        if hasattr(report, "context"):
            self.report["subtests"][report.outcome] += 1
            return
        key = node_hash(report.nodeid)
        phases = self.report["cases"].setdefault(key, {})
        if report.when in phases:
            self.report["report_errors"] += 1
        phases[report.when] = {"duration": report.duration, "outcome": report.outcome}

    def pytest_sessionfinish(self, session, exitstatus):
        self.report.update(exitstatus=int(exitstatus), session_seconds=time.monotonic() - self.started)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.report, indent=2) + "\n")
