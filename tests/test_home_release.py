"""Cover retries around the irreversible node hook using a temporary node journal."""

import importlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("poll_home_release")


def test_interrupted_deploy_resumes_by_verifying_only(receiver, tmp_path):
    sha, live = "a" * 40, "b" * 40
    hook = tmp_path / "adapter"
    log = tmp_path / "operations"
    hook.write_text('#!/bin/sh\necho "$1" >> "$OPS_LOG"\n'
                    f'case "$1" in current) echo {live};; deploy) exit 1;; esac\n')
    hook.chmod(0o700)
    request = dict(id=123, sha=sha, created_at=datetime.now(UTC).isoformat(),
                   payload=dict(ci_run=7, ci_attempt=1))

    def api(path):
        if path.startswith("deployments?"):
            return [request]
        if "/statuses" in path:
            return []
        assert path == f"compare/{live}...{sha}"
        return {"merge_base_commit": {"sha": live}}

    with (
        patch.dict(os.environ, OPS_LOG=str(log)),
        patch.object(receiver, "api", side_effect=api),
        patch.object(receiver, "verified_run", return_value=dict(id=7, run_attempt=1)),
        patch.object(receiver, "approved", return_value=True),
        patch.object(receiver, "report") as report,
    ):
        with pytest.raises(subprocess.CalledProcessError):
            receiver.poll(tmp_path, str(hook))
        receiver.poll(tmp_path, str(hook))
        assert log.read_text().splitlines() == ["current", "deploy", "verify"]
        report.assert_called_with(123, "success")


def test_receipt_must_match_approved_request_id(receiver, tmp_path):
    sha = "a" * 40
    payload = dict(approval_run=1, ci_run=2, ci_attempt=1, schema=1,
                   repository=receiver.REPO)
    request = dict(sha=sha, id=42, payload=payload)
    run = dict(path=".github/workflows/home-release.yml", event="workflow_dispatch",
               head_branch="main", run_attempt=1, display_title=f"Home release {sha}",
               status="completed", conclusion="success")

    def download(command, check):
        receipt = dict(deployment=41, sha=sha, approval_run=1, ci_run=2, ci_attempt=1)
        Path(command[-1], "lead-home-request.json").write_text(receiver.json.dumps(receipt))

    with (
        patch.object(receiver, "api", return_value=run),
        patch.object(receiver.subprocess, "run", side_effect=download),
        pytest.raises(ValueError, match="Mismatch"),
    ):
        receiver.approved(request)


def test_completed_request_does_not_need_expired_approval_artifacts(receiver, tmp_path):
    request = dict(id=123, sha="a" * 40)
    with (
        patch.object(receiver, "api", side_effect=[[request], [{"state": "success"}]]),
        patch.object(receiver, "approved") as approval,
    ):
        receiver.poll(tmp_path, "/unused")
        approval.assert_not_called()
