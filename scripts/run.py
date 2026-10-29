"""Local supervisor. All app ports bind loopback; logs contain no access payloads.
Usage: .venv/bin/python scripts/run.py start|stop|status
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("LEAD_STATE_DIR") or ROOT / ".state").resolve()
PYTHON = ROOT / ".venv/bin/python"
PID = STATE / "supervisor.pid"


def command_for(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    )
    return result.stdout.strip()


def current_pid():
    if not PID.exists():
        return None
    try:
        pid = int(PID.read_text())
        return (
            pid
            if str(ROOT / "scripts/run.py") in command_for(pid)
            and "supervise" in command_for(pid)
            else None
        )
    except (ValueError, ProcessLookupError):
        return None


def occupied(port):
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def supervise():
    os.chdir(ROOT)
    STATE.mkdir(exist_ok=True, mode=0o700)
    PID.write_text(str(os.getpid()))
    PID.chmod(0o600)
    process_env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "services/api"),
        "PYTHONUNBUFFERED": "1",
    }
    services = {
        "api": (
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "lead_app.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8088",
                "--no-access-log",
            ],
            ROOT,
        ),
        "browser": (
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                "lead_app.browser:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8089",
                "--no-access-log",
            ],
            ROOT,
        ),
        "worker": ([str(PYTHON), "-m", "lead_app.worker"], ROOT),
        "maintenance": ([str(PYTHON), "-m", "lead_app.maintenance"], ROOT),
        "web": (
            [
                "node",
                str(ROOT / "apps/web/node_modules/next/dist/bin/next"),
                "start",
                "--hostname",
                "127.0.0.1",
                "--port",
                "3100",
            ],
            ROOT / "apps/web",
        ),
    }
    children = {}
    files = {}
    stopping = False

    def halt(*args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    try:
        for name in services:
            files[name] = (STATE / (name + ".log")).open("a")
            os.chmod(files[name].name, 0o600)
        while not stopping:
            for name, (cmd, cwd) in services.items():
                if name not in children or children[name].poll() is not None:
                    children[name] = subprocess.Popen(
                        cmd,
                        cwd=cwd,
                        env=process_env,
                        stdout=files[name],
                        stderr=subprocess.STDOUT,
                    )
            try:
                (STATE / "processes.next").write_text(
                    json.dumps({name: p.pid for name, p in children.items()})
                )
                os.replace(STATE / "processes.next", STATE / "processes.json")
            except OSError:
                # Status-file failure must not kill otherwise recoverable services.
                pass
            for stream in files.values():
                try:
                    if os.fstat(stream.fileno()).st_size > 20 * 1024**2:
                        stream.truncate(0)
                except OSError:
                    pass
            time.sleep(2)
    finally:
        for p in children.values():
            if p.poll() is None:
                p.terminate()
        for p in children.values():
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in files.values():
            f.close()
        PID.unlink(missing_ok=True)


def start():
    from dotenv import dotenv_values

    settings = {**dotenv_values(ROOT / ".env"), **os.environ}
    if settings.get("LEAD_DEPLOYMENT_MODE") == "production":
        raise SystemExit(
            "Use the production service entries; local start uses a development Temporal service."
        )
    if current_pid():
        print("lead is already running: http://127.0.0.1:3100")
        return
    if any(occupied(p) for p in (3100, 8088, 8089)):
        raise SystemExit(
            "A lead port is in use. Stop the existing instance before starting the supervisor."
        )
    subprocess.run(
        [str(PYTHON), str(ROOT / "scripts/bootstrap.py")], check=True, cwd=ROOT
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(STATE / "infra.env"),
            "-f",
            "infra/compose.yaml",
            "up",
            "-d",
        ],
        check=True,
        cwd=ROOT,
    )
    for _ in range(30):
        if occupied(55432):
            break
        time.sleep(1)
    subprocess.run(
        [str(PYTHON), str(ROOT / "scripts/migrate.py")], check=True, cwd=ROOT
    )
    if not (ROOT / "apps/web/.next/BUILD_ID").exists():
        subprocess.run(["npm", "run", "build"], cwd=ROOT / "apps/web", check=True)
    with (STATE / "supervisor.log").open("a") as output:
        p = subprocess.Popen(
            [str(PYTHON), str(ROOT / "scripts/run.py"), "supervise"],
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    for _ in range(30):
        if occupied(3100):
            subprocess.run(
                [str(PYTHON), str(ROOT / "scripts/soak.py"), "resume"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                check=True,
            )
            print("lead is running: http://127.0.0.1:3100")
            return
        if p.poll() is not None:
            raise SystemExit("Startup failed. See .state/supervisor.log")
        time.sleep(1)
    raise SystemExit("Startup is taking longer than expected. See .state/*.log")


def stop():
    pid = current_pid()
    if not pid:
        print("lead supervisor is not running.")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(25):
        if not current_pid():
            print("lead stopped. Database and workflow data are preserved.")
            return
        time.sleep(1)
    print("lead is completing shutdown. Check .state/supervisor.log")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "supervise":
        supervise()
    elif action == "start":
        start()
    elif action == "stop":
        stop()
    elif action == "status":
        print("running" if current_pid() else "stopped")
    else:
        raise SystemExit("Usage: run.py start|stop|status")
