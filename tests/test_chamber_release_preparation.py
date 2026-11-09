import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from infra.chamber_pc import prepare_release


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "resident.py"
    source.write_bytes(b"verified source\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "resident.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    source.write_bytes(b"uncommitted change\n")
    monkeypatch.setattr(prepare_release, "ROOT", repo)
    monkeypatch.setattr(prepare_release, "SOURCES", {"modules/resident.py": "resident.py"})

    bundle = b"verified workspace"
    runs = [{"headSha": sha, "databaseId": 17, "status": "completed", "conclusion": "success",
             "url": "https://example.org/actions/runs/17"}]
    files = {"policy.tar.gz": bundle, "REVISION": sha.encode(),
             "policy.SHA256": hashlib.sha256(bundle).hexdigest().encode()}
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[0] != "gh":
            assert args[0] == "git"
            return real_run(args, **kwargs)
        assert args[args.index("--repo") + 1] == prepare_release.REPO
        if args[1:3] == ["run", "list"]:
            for option, value in [("--commit", sha), ("--event", "push"), ("--workflow", "ci.yml")]:
                assert args[args.index(option) + 1] == value
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(runs))
        assert args[1:4] == ["run", "download", "17"]
        assert args[args.index("--name") + 1] == f"chamber-workspace-{sha}"
        dest = Path(args[args.index("--dir") + 1])
        for name, data in files.items():
            (dest / name).write_bytes(data)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return sha, runs, files, tmp_path / "releases"


@pytest.mark.parametrize("workspace", [True, False])
def test_prepares_exact_committed_source_and_requested_artifact(candidate, workspace):
    sha, runs, files, parent = candidate
    dest = prepare_release.prepare("HEAD", parent, workspace=workspace)
    manifest = json.loads((dest / "release.json").read_text())
    assert manifest["commit"] == sha and manifest["ci"] == runs[0]["url"]
    assert (dest / "modules/resident.py").read_bytes() == b"verified source\n"
    hashes = {"modules/resident.py": hashlib.sha256(b"verified source\n").hexdigest()}
    if workspace:
        assert (dest / "policy.tar.gz").read_bytes() == files["policy.tar.gz"]
        hashes["policy.tar.gz"] = files["policy.SHA256"].decode()
    else:
        assert not (dest / "policy.tar.gz").exists()
    assert manifest["sha256"] == hashes


@pytest.mark.parametrize("change", [None, {"headSha": "b" * 40}, {"status": "in_progress", "conclusion": ""},
                                     {"conclusion": "failure"}, {"conclusion": "cancelled"}])
def test_requires_successful_ci_for_exact_push(candidate, change):
    _, runs, _, parent = candidate
    if change is None:
        runs.clear()
    else:
        runs[0].update(change)
    with pytest.raises(ValueError, match="CI"):
        prepare_release.prepare("HEAD", parent, workspace=True)
    assert not parent.exists()


@pytest.mark.parametrize("field", ["REVISION", "policy.tar.gz", "policy.SHA256"])
def test_download_must_match_commit_and_digest(candidate, field):
    _, _, files, parent = candidate
    files[field] = b"mismatch"
    with pytest.raises(ValueError, match="提交号|SHA-256"):
        prepare_release.prepare("HEAD", parent, workspace=True)
    assert not list(parent.iterdir())
