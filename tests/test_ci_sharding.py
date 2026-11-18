import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ci_backend_proof as proof

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = {"sha": "a" * 40, "run_id": "123", "run_attempt": "2"}
SECRET = "private-body-and-parameter-93852"


def run_pytest(folder, shard, *, stage="suite", extra=()):
    output = folder / "reports" / f"{stage}-{shard}.json"
    command = [sys.executable, "-m", "pytest", "--noconftest", "-c", "/dev/null",
               "--rootdir", str(folder), "-p", "scripts.ci_pytest", "-q",
               "--ci-shard", str(shard), "--ci-stage", stage, "--ci-report", str(output)]
    paths = proof.EARLY_NODEIDS[:2] + ("tests/test_research_recovery_integration.py",) if stage == "early" else ("tests",)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
           "GITHUB_SHA": IDENTITY["sha"], "GITHUB_RUN_ID": IDENTITY["run_id"],
           "GITHUB_RUN_ATTEMPT": IDENTITY["run_attempt"]}
    result = subprocess.run([*command, *extra, *paths], cwd=folder, env=env,
                            text=True, capture_output=True, timeout=20)
    assert output.exists(), result.stdout + result.stderr
    return result, json.loads(output.read_text())


def make_project(folder):
    tests = folder / "tests"
    tests.mkdir()
    for index, node in enumerate(proof.EARLY_NODEIDS[:2]):
        file, name = node.split("::")
        (folder / file).write_text(f"def {name}():\n    assert True\n")
    (tests / "test_research_recovery_integration.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', [False, True], ids=['both-current', 'one-expired'])\n"
        "def test_original_source_recovery_through_real_outbox_temporal_and_current_dependencies(value):\n"
        "    assert value in (False, True)\n")
    owners = set()
    for index in range(100):
        file = f"tests/test_fixture_{index}.py"
        shard = proof.owner(file)
        if shard in owners:
            continue
        owners.add(shard)
        (folder / file).write_text(
            "import pytest\n"
            f"@pytest.mark.parametrize('value', ['{SECRET}'])\n"
            "def test_pass(value, subtests):\n"
            "    for part in range(2):\n"
            "        with subtests.test(part=part):\n"
            "            assert part < 2\n"
            "def test_setup_skip():\n"
            "    pytest.skip('private skip body')\n")
        if len(owners) == proof.SHARD_COUNT:
            break
    assert owners == set(range(proof.SHARD_COUNT))
    return folder


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


@pytest.fixture(scope="module")
def reports(tmp_path_factory):
    project = make_project(tmp_path_factory.mktemp("ci-shards"))
    values = []
    for shard in range(proof.SHARD_COUNT):
        result, report = run_pytest(project, shard)
        assert result.returncode == 0, result.stdout + result.stderr
        values.append(report)
    result, report = run_pytest(project, 0, stage="early")
    assert result.returncode == 0, result.stdout + result.stderr
    return [*values, report]


def test_real_shards_cover_full_collection_once_and_keep_early_order(reports):
    summary = proof.verify(reports, IDENTITY)
    assert summary["tests"] == 12
    assert summary["early_repeated_tests"] == 4
    assert summary["outcomes"] == {"passed": 8, "skipped": 4}
    assert summary["subtests"] == {"passed": 8, "failed": 0, "skipped": 0}
    assert set(summary["phase_seconds"]) == set(proof.PHASES)
    assert all(seconds >= 0 for seconds in summary["phase_seconds"].values())
    serial_order = [item["id"] for item in reports[0]["collection"]]
    for report in reports[:4]:
        assert report["selected"] == [key for key in serial_order if key in report["selected"]]
        assert report["report_errors"] == 0
    text = json.dumps(reports)
    assert SECRET not in text and "private skip body" not in text


@pytest.mark.parametrize("change", [
    "missing", "extra", "duplicate_shard", "sha", "run_id", "run_attempt",
    "collection", "duplicate_collection", "selection", "missing_case", "unexpected_case",
    "missing_phase", "failed_phase", "bad_duration", "duplicate_phase", "collect_only",
    "exitstatus", "missing_early", "foreign_early", "subtest_failure", "early_skip", "early_metadata",
])
def test_aggregate_rejects_incomplete_or_foreign_reports(reports, change):
    values = copy.deepcopy(reports)
    report = values[0]
    test_id = report["selected"][0]
    if change == "missing":
        values.pop(1)
    elif change == "extra":
        values.append(copy.deepcopy(report))
    elif change == "duplicate_shard":
        values[1]["shard"] = 0
    elif change in {"sha", "run_id", "run_attempt"}:
        report["identity"][change] = "b" * 40 if change == "sha" else "9"
    elif change == "collection":
        report["collection"].pop()
    elif change == "duplicate_collection":
        report["collection"].append(copy.deepcopy(report["collection"][0]))
    elif change == "selection":
        report["selected"].append(test_id)
    elif change == "missing_case":
        report["cases"].pop(test_id)
    elif change == "unexpected_case":
        report["cases"]["f" * 64] = report["cases"][test_id]
    elif change == "missing_phase":
        report["cases"][test_id].pop("teardown")
    elif change == "failed_phase":
        report["cases"][test_id]["teardown"]["outcome"] = "failed"
    elif change == "bad_duration":
        report["cases"][test_id]["setup"]["duration"] = float("nan")
    elif change == "duplicate_phase":
        report["report_errors"] = 1
    elif change == "collect_only":
        report["collect_only"] = True
    elif change == "exitstatus":
        report["exitstatus"] = 1
    elif change == "missing_early":
        values[-1]["stage"] = "suite"
    elif change == "foreign_early":
        values[-1]["selected"].pop()
    elif change == "subtest_failure":
        report["subtests"]["failed"] = 1
    elif change == "early_skip":
        values[-1]["cases"][values[-1]["selected"][0]]["call"]["outcome"] = "skipped"
    elif change == "early_metadata":
        values[-1]["collection"][0]["file"] = "tests/test_changed.py"
    with pytest.raises(ValueError):
        proof.verify(values, IDENTITY)


@pytest.mark.parametrize("failure", ["call", "setup", "teardown", "subtest", "collection"])
def test_actual_failed_sessions_keep_sanitized_diagnostics(project, failure):
    file = project / "tests/test_failure.py"
    if failure == "collection":
        source = f"raise RuntimeError('{SECRET}')\n"
    elif failure == "call":
        source = f"def test_failure():\n    print('{SECRET}')\n    assert False, '{SECRET}'\n"
    elif failure == "subtest":
        source = f"def test_failure(subtests):\n    with subtests.test(body='{SECRET}'):\n        assert False, '{SECRET}'\n"
    else:
        body = f"    raise RuntimeError('{SECRET}')\n"
        source = ("import pytest\n@pytest.fixture\ndef broken():\n" +
                  ("    yield\n" + body if failure == "teardown" else body) +
                  "def test_failure(broken):\n    pass\n")
    file.write_text(source)
    result, report = run_pytest(project, proof.owner("tests/test_failure.py"), extra=("--maxfail=1",))
    assert result.returncode != 0
    assert report["exitstatus"] != 0
    assert SECRET not in json.dumps(report)
    if failure == "subtest":
        assert report["subtests"]["failed"] == 1


def test_collect_only_can_audit_selection_but_cannot_release(project):
    reports = []
    for shard in range(proof.SHARD_COUNT):
        result, report = run_pytest(project, shard, extra=("--collect-only",))
        assert result.returncode == 0
        assert report["cases"] == {}
        reports.append(report)
    assert len(proof.validate_collection(reports)) == 12
    _, early = run_pytest(project, 0, stage="early", extra=("--collect-only",))
    with pytest.raises(ValueError, match="Incomplete"):
        proof.verify([*reports, early], IDENTITY)


def test_pytest_filter_cannot_silently_reduce_coverage(project, reports):
    result, filtered = run_pytest(project, 0, extra=("-k", "test_pass"))
    assert result.returncode == 0
    assert len(filtered["collection"]) == 12
    reports = [filtered, *reports[1:]]
    with pytest.raises(ValueError, match="selection"):
        proof.verify(reports, IDENTITY)


def test_cli_reads_exact_report_set_and_rejects_stale_extra_file(project, reports):
    folder = project / "reports"
    folder.mkdir()
    for index, report in enumerate(reports):
        (folder / f"report-{index}.json").write_text(json.dumps(report))
    env = {**os.environ, "GITHUB_SHA": IDENTITY["sha"], "GITHUB_RUN_ID": IDENTITY["run_id"],
           "GITHUB_RUN_ATTEMPT": IDENTITY["run_attempt"]}
    command = [sys.executable, str(ROOT / "scripts/ci_backend_proof.py"), str(project / "reports")]
    valid = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["tests"] == 12
    (project / "reports/stale.json").write_text(json.dumps(reports[0]))
    invalid = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert invalid.returncode != 0
