"""Poll GitHub from the single home release node; default to read-only inspection."""

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from request_home_release import REPO, api, github_cli, verified_run


def read_receipt(run_id):
    listing = api(f"actions/runs/{run_id}/artifacts?name=lead-home-request&per_page=2")
    if listing["total_count"] != 1 or len(listing["artifacts"]) != 1:
        raise ValueError("Missing or ambiguous approval artifact")
    item = listing["artifacts"][0]
    if item["name"] != "lead-home-request" or item["expired"] or not 0 < item["size_in_bytes"] < 65536:
        raise ValueError("Invalid approval artifact")
    raw = subprocess.check_output([*github_cli(), "api", f"repos/{REPO}/actions/artifacts/{item['id']}/zip"])
    if "sha256:" + hashlib.sha256(raw).hexdigest() != item["digest"]:
        raise ValueError("Invalid approval artifact digest")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = archive.infolist()
        if len(files) != 1 or files[0].filename != "lead-home-request.json" or files[0].file_size > 2048:
            raise ValueError("Invalid approval receipt")
        return json.loads(archive.read(files[0]))


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
    receipt = read_receipt(run_id)
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


def save_record(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        f.write(value)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def finish(directory, hook, request_id, sha):
    if (directory / f"{request_id}.started").read_text() != sha:
        raise ValueError("Saved request does not match GitHub")
    result = directory / f"{request_id}.result"
    if not result.exists():
        passed = subprocess.run([hook, "verify", sha], check=False).returncode == 0
        save_record(result, "success" if passed else "error")
    outcome = result.read_text()
    if outcome not in {"success", "error"}:
        raise ValueError("Invalid saved verification result")
    report(request_id, outcome)
    save_record(directory / f"{request_id}.reported", outcome)
    if outcome == "error":
        raise ValueError("Node verification failed; inspect before the next release")


def poll(directory, hook):
    requests = api("deployments?environment=home-production&task=lead-home-release&per_page=1")
    if not requests:
        print("No home deployment request")
        return
    request = requests[0]
    request_id, sha = request["id"], request["sha"]
    print(f"Latest home deployment #{request_id}, SHA {sha}")
    statuses = api(f"deployments/{request_id}/statuses?per_page=1")
    remote = statuses[0]["state"] if statuses else None
    if hook is None:
        if remote not in {"success", "inactive", "failure", "error"}:
            approved(request)
        return
    # Resolve a prior cutover first, even if GitHub now lists a newer request.
    for previous in sorted(directory.glob("*.started")):
        prior_id = int(previous.stem)
        if prior_id != request_id and not previous.with_suffix(".reported").exists():
            prior = api(f"deployments/{prior_id}")
            if prior["sha"] != previous.read_text() or prior["id"] != prior_id:
                raise ValueError("Prior deployment mismatch")
            states = api(f"deployments/{prior_id}/statuses?per_page=1")
            state = states[0]["state"] if states else None
            if state in {"success", "inactive", "failure", "error"}:
                save_record(previous.with_suffix(".reported"), state)
            else:
                finish(directory, hook, prior_id, prior["sha"])
            return
    marker = directory / f"{request_id}.started"
    if remote in {"success", "inactive", "failure", "error"}:
        if marker.exists() and not marker.with_suffix(".reported").exists():
            save_record(marker.with_suffix(".reported"), remote)
        return
    if not marker.exists():
        if not approved(request):
            return
        if remote == "in_progress":
            raise ValueError("Another node started this request; inspect it first")
        if datetime.now(UTC) - datetime.fromisoformat(request["created_at"]) > timedelta(days=1):
            raise ValueError("Request is over 24 hours old; request fresh approval")
        ci = verified_run(sha)
        if (ci["id"], ci["run_attempt"]) != (request["payload"]["ci_run"], request["payload"]["ci_attempt"]):
            raise ValueError("Approved CI run or attempt changed")
        live = subprocess.check_output([hook, "current"], text=True).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", live):
            raise ValueError("Node did not identify its current version")
        if live != sha and api(f"compare/{live}...{sha}")["merge_base_commit"]["sha"] != live:
            raise ValueError("Release would roll back or discard a divergent live version")
        save_record(marker, sha)
        report(request_id, "in_progress")
        if live != sha:
            subprocess.run([hook, "deploy", sha], check=True)
    # An interrupted request resumes with an independent probe, never a new cutover.
    finish(directory, hook, request_id, sha)


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
