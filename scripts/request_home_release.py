"""Issue a versioned release request after exact main CI and GitHub approval."""

import json
import os
import re
import subprocess
from pathlib import Path

REPO = "zdaiwmm/lead"


def github_cli(*, node=False):
    if not node and os.environ.get("LEAD_NODE_GITHUB") != "1":
        return ["gh"]
    import pwd

    user = pwd.getpwnam("lead-release-fetch")
    return ["/usr/bin/setpriv", "--reuid", str(user.pw_uid), "--regid", str(user.pw_gid),
            "--clear-groups", "--no-new-privs", "--", "/usr/bin/env", "-i", "HOME=" + user.pw_dir,
            "PATH=/usr/bin:/bin", "GH_PROMPT_DISABLED=1", "/usr/local/lib/lead-home-release/gh"]


def api(resource, payload=None):
    args = [*github_cli(), "api", f"repos/{REPO}/{resource}"]
    if payload is not None:
        args.extend(["--method", "POST", "--input", "-"])
    output = subprocess.check_output(
        args, input=json.dumps(payload) if payload is not None else None, text=True, timeout=45
    )
    return json.loads(output)


def verified_run(sha, *, get=None, kind="application"):
    if get is None:
        get = api
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected a full commit SHA")
    comparison = get(f"compare/{sha}...main")
    if comparison["merge_base_commit"]["sha"] != sha:
        raise ValueError("Commit is outside main")
    runs = get(
        f"actions/workflows/ci.yml/runs?branch=main&head_sha={sha}&per_page=100"
    )
    if runs["total_count"] > 100:
        raise ValueError("CI listing incomplete")
    candidates = [
        run for run in runs["workflow_runs"]
        if run["head_sha"] == sha and run["head_branch"] == "main"
        and run["path"] == ".github/workflows/ci.yml"
        and run["event"] in {"push", "workflow_dispatch"}
    ]
    run = max(candidates, key=lambda r: r["id"], default=None)
    if not run or (run["status"], run["conclusion"]) != ("completed", "success"):
        raise ValueError("Latest exact main CI has not passed")
    jobs = get(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100")
    if kind not in {"application", "tools"}:
        raise ValueError("Unknown release kind")
    if jobs.get("total_count", len(jobs["jobs"])) > 100:
        raise ValueError("Verification jobs listing incomplete")
    named = {j["name"]: j for j in jobs["jobs"]}
    if len(named) != len(jobs["jobs"]):
        raise ValueError("Ambiguous verification jobs")
    passed = {name for name, job in named.items() if job["conclusion"] == "success"}
    if "scope" in named or "verified" in named:
        proof = named.get("verified", {})
        steps = {s["name"] for s in proof.get("steps", []) if s.get("conclusion") == "success"}
        if not {"scope", "checks", "verified"} <= passed:
            raise ValueError("Required verification proof has not passed")
        application = "Application verification" in steps and {"backend", "frontend", "namespace"} <= passed
        tools = "Tool verification" in steps and {"delivery", "namespace"} <= passed
        if kind == "application" and not application:
            raise ValueError("Application verification required; tool/docs/PR checks cannot release an application")
        if kind == "tools" and (not (tools or application) or "tool-bundle" not in passed):
            raise ValueError("Tool verification and tested bundle required")
    elif kind != "application" or not {"backend", "frontend", "namespace"} <= passed:
        # Historical exact application runs remain usable; they never prove a tool bundle.
        raise ValueError("Required verification jobs have not passed")
    return run


def main():
    sha = os.environ["APPROVED_SHA"]
    run = verified_run(sha)
    prs = api(f"commits/{sha}/pulls?per_page=100")
    merged = next((pr for pr in prs if pr["merged_at"] and pr["merge_commit_sha"] == sha
                   and pr["base"]["ref"] == "main" and pr["base"]["repo"]["full_name"] == REPO), None)
    if merged is None:
        raise ValueError("Release SHA must be the result of a merged PR into main")
    release = api("deployments", {
        "ref": sha, "task": "lead-home-release", "auto_merge": False,
        "environment": "home-production", "production_environment": True,
        "required_contexts": [],
        "description": "Approved version awaiting the home deployment agent",
        "payload": {"schema": 1, "repository": REPO, "ci_run": run["id"],
                    "ci_attempt": run["run_attempt"],
                    "approval_run": int(os.environ["GITHUB_RUN_ID"])},
    })
    if release.get("sha") != sha or not isinstance(release.get("id"), int):
        raise ValueError("Unresolved deployment request; inspect GitHub before retrying")
    receipt = {"deployment": release["id"], "sha": sha, "ci_run": run["id"],
               "ci_attempt": run["run_attempt"], "approval_run": int(os.environ["GITHUB_RUN_ID"])}
    Path(os.environ["RUNNER_TEMP"], "lead-home-request.json").write_text(json.dumps(receipt))
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as file:
        file.write(f"Home deployment request #{release['id']} for `{sha}` (merged PR #{merged['number']}) created. "
                   "Check deployment statuses for the server result.\n")


if __name__ == "__main__":
    main()
