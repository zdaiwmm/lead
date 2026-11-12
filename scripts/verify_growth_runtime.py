"""Own disposable verification services; never use the user's default state."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "contracts": ["tests/test_growth_contracts.py", "tests/test_growth_store.py", "tests/test_growth_migrations.py"],
    "decision": ["tests/test_growth_decision.py", "tests/test_growth_product_understanding.py", "tests/test_growth_progress.py", "tests/test_growth_review_regressions.py"],
    "runtime": ["tests/test_growth_runtime.py", "tests/test_growth_workflows.py", "tests/test_growth_concurrency.py", "tests/test_growth_recovery.py", "tests/test_growth_liveness.py", "tests/test_growth_shared_outage.py"],
    "account": ["tests/test_growth_account_connection.py", "tests/test_growth_execution_routes.py", "tests/test_growth_controls.py", "tests/test_growth_identity_platforms.py", "tests/test_growth_profile_takeover.py"],
    "tools": ["tests/test_growth_tools.py", "tests/test_growth_content.py", "tests/test_growth_dependencies.py", "tests/test_growth_research_recovery.py"],
    "outcomes": ["tests/test_growth_outcomes.py", "tests/test_growth_attribution.py", "tests/test_growth_costs.py"],
    "submission": ["tests/test_growth_channel_operation.py", "tests/test_growth_verification_proof.py", "tests/test_growth_unknown_receipts.py", "tests/test_growth_verification_scope.py"],
    "ui": ["tests/test_growth_views.py"],
    "migration": ["tests/test_growth_migration.py", "tests/test_growth_legacy_exit.py"],
    "e2e": ["tests/test_growth_e2e.py", "tests/test_growth_generalization.py", "tests/test_growth_liveness.py", "tests/test_growth_task_loop.py"],
}


def validate_state(value):
    value = Path(value).expanduser()
    if not value.is_absolute():
        raise ValueError("An absolute isolated state directory is required")
    if value.is_symlink() or any(parent.is_symlink() for parent in value.parents):
        # /private/tmp is canonical on macOS; reject indirection supplied by callers.
        raise ValueError("An isolated regular directory is required")
    path = value.resolve()
    if path in (ROOT, ROOT / ".state", Path.home()) or "profiles" in path.parts:
        raise ValueError("An isolated state directory is required")
    if path.is_symlink():
        raise ValueError("An isolated regular directory is required")
    return path


def read_manifest(state):
    path = validate_state(state) / "manifest.json"
    if not path.is_file():
        raise ValueError("Verification manifest is missing")
    data = json.loads(path.read_text())
    if data.get("root") != str(ROOT) or data.get("state") != str(state.resolve()):
        raise ValueError("Verification manifest belongs to another workspace")
    if any(not 1024 < data[k] < 65536 or data[k] in (55432, 17233, 8088, 8089) for k in ("pg_port", "temporal_port", "temporal_ui_port")):
        raise ValueError("Verification manifest endpoints are not isolated")
    return data


def environment(state):
    data = read_manifest(state)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LEAD_", "OPENAI_", "DEEPSEEK_", "ANTHROPIC_"))}
    env.update({
        "LEAD_STATE_DIR": str(state / "app"),
        "LEAD_ENV_FILE": str(state / "test.env"),
        "LEAD_DB_HOST": "127.0.0.1",
        "LEAD_DB_PORT": str(data["pg_port"]),
        "LEAD_DB_NAME": "lead_growth_test",
        "LEAD_GROWTH_TEST_ENV": "1",
        "LEAD_WORKFLOW_TESTS": "1",
        "LEAD_BROWSER_TESTS": "1",
        "LEAD_TEST_CHANNEL_TEMPORAL": "127.0.0.1:" + str(data["temporal_port"]),
        "PYTHONPATH": str(ROOT / "services/api") + os.pathsep + str(ROOT),
    })
    config = json.loads((state / "app/config.json").read_text())
    if config["temporal_address"] != "127.0.0.1:" + str(data["temporal_port"]):
        raise ValueError("Verification manifest and effective config differ")
    return env


def tools_directory():
    configured = os.environ.get("LEAD_TEST_TOOLS")
    if configured:
        return Path(configured).resolve()
    common = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=ROOT, text=True).strip()
    return (ROOT / common).resolve().parent / ".state/tools"


def pg_bin():
    candidates = [tools_directory() / "Postgres.app/Contents/Versions/18/bin"]
    if shutil.which("pg_ctl"):
        candidates.append(Path(shutil.which("pg_ctl")).parent)
    for path in candidates:
        if (path / "pg_ctl").is_file():
            return path
    raise ValueError("PostgreSQL 18 test binaries are required")


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def owns_process(pid, needle):
    if not pid:
        return False
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return result.returncode == 0 and needle in result.stdout


def wait_port(port, process=None):
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=.1):
                return
        except OSError:
            if process is not None and process.poll() is not None:
                raise RuntimeError("Test service exited; inspect its private log")
            time.sleep(.1)
    raise RuntimeError("Test service readiness timed out")


def prepare(state):
    state = validate_state(state)
    if (state / "manifest.json").exists():
        data = read_manifest(state)
    else:
        if state.exists() and any(state.iterdir()):
            raise ValueError("Refusing a nonempty unregistered state directory")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        (state / "app").mkdir(mode=0o700)
        ports = set()
        while len(ports) < 3:
            ports.add(free_port())
        pg, temporal, ui = sorted(ports)
        data = {"version": 1, "root": str(ROOT), "state": str(state), "pg_port": pg,
                "temporal_port": temporal, "temporal_ui_port": ui, "temporal_pid": None}
        config = {"owner_id": str(uuid4()), "owner_password": secrets.token_urlsafe(32),
                  "runtime_password": secrets.token_urlsafe(32), "auth_password": secrets.token_urlsafe(32),
                  "session_secret": secrets.token_urlsafe(48), "api_origin": "http://127.0.0.1:8088",
                  "web_origin": "http://localhost:3100", "browser_concurrency": 3,
                  "temporal_address": f"127.0.0.1:{temporal}"}
        (state / "app/config.json").write_text(json.dumps(config))
        (state / "app/config.json").chmod(0o600)
        (state / "test.env").write_text("LEAD_DEPLOYMENT_MODE=development\n")
        (state / "test.env").chmod(0o600)
        (state / "manifest.json").write_text(json.dumps(data, indent=2))
    config = json.loads((state / "app/config.json").read_text())
    pgtools = pg_bin()
    if not (state / "pg/PG_VERSION").exists():
        pwfile = state / "init-password"
        pwfile.write_text(config["owner_password"])
        pwfile.chmod(0o600)
        try:
            subprocess.run([str(pgtools / "initdb"), "-D", str(state / "pg"), "-U", "lead_owner",
                            "-A", "scram-sha-256", "--pwfile", str(pwfile), "--encoding=UTF8", "--locale=C"],
                           check=True, stdout=subprocess.DEVNULL)
        finally:
            pwfile.unlink(missing_ok=True)
    status = subprocess.run([str(pgtools / "pg_ctl"), "-D", str(state / "pg"), "status"], capture_output=True)
    if status.returncode:
        subprocess.run([str(pgtools / "pg_ctl"), "-D", str(state / "pg"), "-l", str(state / "postgres.log"),
                        "-o", f"-h 127.0.0.1 -p {data['pg_port']} -k /tmp", "start", "-w"],
                       check=True, stdout=subprocess.DEVNULL)
    import psycopg
    from psycopg import sql
    with psycopg.connect(host="127.0.0.1", port=data["pg_port"], dbname="postgres",
                         user="lead_owner", password=config["owner_password"], autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='lead_growth_test'").fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier("lead_growth_test")))
    needle = str(state / "temporal.sqlite")
    if not owns_process(data.get("temporal_pid"), needle):
        executable = shutil.which("temporal") or str(tools_directory() / "temporal")
        with (state / "temporal.log").open("ab") as log:
            child = subprocess.Popen([executable, "server", "start-dev", "--ip", "127.0.0.1",
                                      "--port", str(data["temporal_port"]), "--ui-port", str(data["temporal_ui_port"]),
                                      "--db-filename", needle], stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        data["temporal_pid"] = child.pid
        (state / "manifest.json").write_text(json.dumps(data, indent=2))
        wait_port(data["temporal_port"], child)
    subprocess.run([sys.executable, "scripts/migrate.py"], cwd=ROOT, env=environment(state), check=True)
    print(json.dumps({"prepared": True, "database_port": data["pg_port"], "temporal_port": data["temporal_port"]}))


def stop(state):
    data = read_manifest(state)
    if owns_process(data.get("temporal_pid"), str(state / "temporal.sqlite")):
        os.kill(data["temporal_pid"], signal.SIGTERM)
    status = subprocess.run([str(pg_bin() / "pg_ctl"), "-D", str(state / "pg"), "status"], capture_output=True)
    if status.returncode == 0:
        subprocess.run([str(pg_bin() / "pg_ctl"), "-D", str(state / "pg"), "stop", "-m", "fast", "-w"], check=True)
    print("Stopped only this manifest's verification services.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--stop", action="store_true")
    modes.add_argument("--suite", choices=SUITES)
    modes.add_argument("--model-evaluation", action="store_true")
    parser.add_argument("--model-config",type=Path)
    parser.add_argument("--max-units",type=int)
    args = parser.parse_args()
    state = validate_state(args.state_dir)
    if args.prepare:
        prepare(state)
    elif args.stop:
        stop(state)
    elif args.model_evaluation:
        if not args.model_config or not args.max_units:
            raise ValueError("Real model evaluation requires an explicit private model config and finite --max-units")
        raise SystemExit(subprocess.run([sys.executable,str(ROOT/"scripts/evaluate_growth_model.py"),
            "--state-dir",str(state),"--model-config",str(args.model_config),"--max-units",str(args.max_units)],cwd=ROOT,
            env=environment(state)).returncode)
    else:
        files = SUITES[args.suite]
        if any(not (ROOT / name).is_file() for name in files):
            raise ValueError("Verification suite is incomplete; missing tests must not be skipped")
        env = environment(state)
        report = state / "reports"
        report.mkdir(exist_ok=True)
        command = [sys.executable, "-m", "pytest", "-q", *files, "--junitxml", str(report / (args.suite + ".xml"))]
        status = subprocess.run(command, cwd=ROOT, env=env).returncode
        source = subprocess.check_output(["git","ls-files","-co","--exclude-standard","-z"],cwd=ROOT).decode().split("\0")
        hashes = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source
            if name and (ROOT/name).is_file() and not (ROOT/name).is_symlink()}
        manifest = read_manifest(state)
        (report/(args.suite+"-inputs.json")).write_text(json.dumps({"suite":args.suite,"exit_code":status,
            "head":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
            "source_hash":hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(),
            "endpoints":{"postgres":"127.0.0.1:"+str(manifest["pg_port"]),"temporal":"127.0.0.1:"+str(manifest["temporal_port"])},
            "dependencies":{name:importlib.metadata.version(name) for name in ("temporalio","pydantic","psycopg","fastapi","camoufox","playwright")},
            "files":files,"external_social_actions":0},indent=2))
        raise SystemExit(status)


if __name__ == "__main__":
    main()
