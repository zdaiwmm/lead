"""Scope/proof boundaries, including skipped jobs, incomplete history and mixed changes."""

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
scope = importlib.import_module("ci_scope")
gate = importlib.import_module("ci_gate")
release = importlib.import_module("request_home_release")

SHA = "a" * 40


@pytest.mark.parametrize("paths,kind", [
    (["docs/SMALL_FIX_RELEASE.md"], "docs"),
    (["scripts/prepare_home_image.py", "docs/HOME_RELEASE.md"], "tools"),
    (["scripts/home_systemd_release.py", "tests/test_tool_release.py"], "tools"),
    ([".github/workflows/ci.yml", "scripts/ci_scope.py"], "tools"),
    (["scripts/prepare_home_image.py", "services/api/lead_app/auth.py"], "application"),
    (["apps/web/app/page.tsx"], "application"),
    (["scripts/home_live_maintenance.py"], "application"),
    (["scripts/not-registered.py"], "application"),
    (["requirements.lock.txt"], "application"),
    (["docs/SECURITY.md"], "application"),
    (["../AGENTS.md"], "application"),
    ([], "application"),
])
def test_scope_is_complete_and_fail_closed(paths, kind):
    assert scope.classify([(p, "100644", "100644") for p in paths]) == kind


def test_modes_deletions_and_real_git_renames_cannot_hide_runtime_changes(tmp_path):
    assert scope.classify([("AGENTS.md", "100644", "120000")]) == "application"
    assert scope.classify([("scripts/ci_scope.py", "100644", "000000")]) == "application"
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    (tmp_path / "AGENTS.md").write_text("old")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "AGENTS.md").rename(tmp_path / "runtime.py")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-qm", "rename")
    assert scope.classify(scope.changed_files(tmp_path, base, git("rev-parse", "HEAD"))) == "application"
    with pytest.raises(ValueError):
        scope.changed_files(tmp_path, "0" * 40, base)


def test_manual_dispatch_cannot_choose_a_shortcut(tmp_path, monkeypatch):
    event, output = tmp_path / "event.json", tmp_path / "outputs"
    event.write_text(json.dumps({"inputs": {"kind": "docs"}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["ci_scope.py"])
    scope.main()
    assert output.read_text() == "kind=application\ntools=true\n"


def test_pr_scope_excludes_unrelated_main_changes(tmp_path):
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    def commit():
        git("add", ".")
        git("-c", "user.name=test", "-c", "user.email=test@example.test", "commit", "-qm", "fixture")
        return git("rev-parse", "HEAD")
    (tmp_path / "AGENTS.md").write_text("base")
    base = commit()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/prepare_home_image.py").write_text("tool")
    head = commit()
    git("checkout", "-q", "--detach", base)
    (tmp_path / "application.py").write_text("other main task")
    main = commit()
    assert scope.classify(scope.changed_files(tmp_path, main, head)) == "application"
    assert scope.classify(scope.changed_files(tmp_path, main, head, merge_base=True)) == "tools"


@pytest.mark.parametrize("kind,event,required", [
    ("application", "pull_request", {"scope", "checks", "frontend"}),
    ("tools", "pull_request", {"scope", "checks"}),
    ("application", "push", {"scope", "checks", "backend", "frontend", "namespace"}),
    ("tools", "push", {"scope", "checks", "delivery", "namespace"}),
    ("docs", "push", {"scope", "checks"}),
])
def test_gate_checks_required_jobs_even_when_github_overall_would_skip(kind, event, required):
    jobs = {k: "skipped" for k in ("scope", "checks", "backend", "frontend", "namespace", "delivery")}
    jobs.update({k: "success" for k in required})
    gate.verify(kind, event, jobs)
    for job in required:
        for result in ("skipped", "failure", "cancelled"):
            with pytest.raises(ValueError):
                gate.verify(kind, event, {**jobs, job: result})
    with pytest.raises(ValueError):
        gate.verify("docs", "push", {**jobs, "unexpected": "failure"})


def proof_api(kind="tools", *, event="push", conclusion="success", marker=None):
    names = {"scope", "checks", "verified", "namespace", "tool-bundle"}
    names |= {"delivery"} if kind == "tools" else {"backend", "frontend"}
    step = marker or ("Tool verification" if kind == "tools" else "Application verification")
    jobs = [{"name": n, "conclusion": "success", "steps":
             [{"name": step, "conclusion": "success"}] if n == "verified" else []} for n in names]
    def get(path):
        if path.startswith("compare/"):
            return {"merge_base_commit": {"sha": SHA}}
        if path.startswith("actions/workflows/"):
            return {"total_count": 1, "workflow_runs": [{"id": 7, "run_attempt": 1,
                    "head_sha": SHA, "head_branch": "main", "path": ".github/workflows/ci.yml",
                    "event": event, "status": "completed", "conclusion": conclusion}]}
        assert path == "actions/runs/7/attempts/1/jobs?per_page=100"
        return {"total_count": len(jobs), "jobs": jobs}
    return get, jobs


def test_tool_proof_cannot_release_an_application():
    get, _ = proof_api()
    assert release.verified_run(SHA, get=get, kind="tools")["id"] == 7
    with pytest.raises(ValueError, match="Application verification required"):
        release.verified_run(SHA, get=get)


@pytest.mark.parametrize("event,conclusion", [("pull_request", "success"), ("push", "failure"),
                                               ("push", "cancelled"), ("push", None)])
def test_old_green_cannot_override_pr_or_new_failed_result(event, conclusion):
    get, _ = proof_api(event=event, conclusion=conclusion)
    with pytest.raises(ValueError):
        release.verified_run(SHA, get=get, kind="tools")


def test_docs_marker_or_skipped_tool_jobs_cannot_supply_release_proof():
    get, jobs = proof_api(marker="Documentation verification")
    with pytest.raises(ValueError):
        release.verified_run(SHA, get=get, kind="tools")
    get, jobs = proof_api()
    for job in jobs:
        if job["name"] == "namespace":
            job["conclusion"] = "skipped"
    with pytest.raises(ValueError):
        release.verified_run(SHA, get=get, kind="tools")


def test_application_verification_is_accepted_without_repeating_full_pr_ci():
    get, jobs = proof_api(kind="application")
    assert release.verified_run(SHA, get=get)["id"] == 7
    assert release.verified_run(SHA, get=get, kind="tools")["id"] == 7
    jobs[:] = [j for j in jobs if j["name"] != "tool-bundle"]
    assert release.verified_run(SHA, get=get)["id"] == 7
    with pytest.raises(ValueError, match="Tool verification"):
        release.verified_run(SHA, get=get, kind="tools")


def test_workflow_contract_connects_scope_gate_tests_and_builds():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "pull_request" in workflow["on"] and "workflow_dispatch" in workflow["on"]
    assert "github.event_name != 'pull_request'" in jobs["backend"]["if"]
    assert "github.event_name != 'pull_request'" in jobs["delivery"]["if"]
    assert jobs["verified"]["if"] == "always()"
    assert set(jobs["verified"]["needs"]) == {"scope", "checks", "backend", "frontend", "namespace", "delivery"}
    assert jobs["tool-bundle"]["needs"] == ["scope", "verified"]
    assert "needs.scope.outputs.tools == 'true'" in jobs["tool-bundle"]["if"]
    assert "needs.scope.outputs.kind != 'docs'" in jobs["namespace"]["if"]
    steps = jobs["namespace"]["steps"]
    assert any("tests/home_mount_drill.py" in s.get("run", "") for s in steps)
    assert any("tests/tool_install_drill.py" in s.get("run", "") for s in steps)
    assert any("LEAD_BROWSER_TESTS=1" in s.get("run", "") and "pytest" in s.get("run", "")
               for s in jobs["backend"]["steps"])
    assert scope.TOOL_FILES == importlib.import_module("home_systemd_release").OPERATOR_ONLY_FILES
