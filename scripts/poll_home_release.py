"""Poll GitHub from the single home release node; default to read-only inspection."""

import argparse
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from request_home_release import REPO, api, verified_run


def approved(request):
    sha = request["sha"]
    payload = request["payload"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not isinstance(payload, dict):
        raise ValueError("Invalid deployment request")
    run_id = payload["approval_run"]
    if type(run_id) is not int or run_id < 1:
        raise ValueError("Invalid approval run")
    run = api(f"actions/runs/{run_id}")
    if (run["path"], run["event"], run["head_branch"], run["run_attempt"],
            run["display_title"]) != (
        ".github/workflows/home-release.yml", "workflow_dispatch", "main", 1,
        f"Home release {sha}",
    ):
        raise ValueError("Unexpected approval workflow")
    if run["status"] != "completed":
        return False
    if run["conclusion"] != "success":
        raise ValueError("Approval workflow incomplete or failed")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["gh", "run", "download", str(run_id), "--repo", REPO,
                        "--name", "lead-home-request", "--dir", tmp], check=True)
        receipt = json.loads(Path(tmp, "lead-home-request.json").read_text())
    expected = dict(deployment=request["id"], sha=sha, ci_run=payload["ci_run"],
                    ci_attempt=payload["ci_attempt"], approval_run=run_id)
    if payload.get("schema") != 1 or payload.get("repository") != REPO or receipt != expected:
        raise ValueError("Mismatch between GitHub approval and deployment request")
    return True


def report(request_id, state):
    api(f"deployments/{request_id}/statuses", {
        "state": state, "auto_inactive": False,
        "description": {"in_progress": "Node accepted approved version",
                        "success": "Node independently verified the running version",
                        "error": "Node requires inspection before further deployment"}[state],
    })


def poll(directory, hook):
    requests = api("deployments?environment=home-production&task=lead-home-release&per_page=1")
    if not requests:
        print("No home deployment request")
        return
    request = requests[0]
    request_id, sha = request["id"], request["sha"]
    print(f"Latest home deployment #{request_id}, SHA {sha}")
    if not approved(request) or hook is None:
        return
    statuses = api(f"deployments/{request_id}/statuses?per_page=1")
    remote = statuses[0]["state"] if statuses else None
    if remote in {"success", "inactive", "failure", "error"}:
        return
    marker = directory / f"{request_id}.started"
    if not marker.exists():
        if remote == "in_progress":
            raise ValueError("Another node started this request; inspect it first")
        if datetime.now(UTC) - datetime.fromisoformat(request["created_at"]) > timedelta(days=1):
            raise ValueError("Request is over 24 hours old; request fresh approval")
        verified_run(sha)
        live = subprocess.check_output([hook, "current"], text=True).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", live):
            raise ValueError("Node did not identify its current version")
        if live != sha and api(f"compare/{live}...{sha}")["merge_base_commit"]["sha"] != live:
            raise ValueError("Release would roll back or discard a divergent live version")
        with marker.open("x") as file:
            file.write(sha)
            file.flush()
            os.fsync(file.fileno())
        # Persist the directory entry before calling a hook with side effects.
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        report(request_id, "in_progress")
        if live != sha:
            subprocess.run([hook, "deploy", sha], check=True)
    # An interrupted request resumes with an independent probe, never a new cutover.
    if marker.read_text() != sha:
        raise ValueError("Saved request does not match GitHub")
    result = subprocess.run([hook, "verify", sha], check=False)
    report(request_id, "success" if result.returncode == 0 else "error")
    result.check_returncode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook", type=Path, help="Enable execution with a trusted local adapter")
    parser.add_argument("--state-dir", type=Path, help="Private persistent node release journal")
    args = parser.parse_args()
    if args.hook is None:
        poll(None, None)
        return
    if not args.hook.is_absolute() or not args.state_dir or not args.state_dir.is_absolute():
        parser.error("Execution requires an absolute hook and persistent state directory")
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (args.state_dir / "poll.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        poll(args.state_dir, str(args.hook))


if __name__ == "__main__":
    main()
