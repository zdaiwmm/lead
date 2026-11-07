"""Loopback development data-worker supervisor; never starts social tasks."""

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("LEAD_STATE_DIR") or ROOT / ".state").resolve()
PID = STATE / "analytics-supervisor.pid"


def current_pid():
    try:
        pid = int(PID.read_text())
        args = psutil.Process(pid).cmdline()
        return (
            pid
            if str(Path(__file__).resolve()) in args and "supervise" in args
            else None
        )
    except (OSError, ValueError, psutil.Error):
        return None


def _local_only():
    from dotenv import dotenv_values

    if ({**dotenv_values(ROOT / ".env"), **os.environ}).get(
        "LEAD_DEPLOYMENT_MODE"
    ) == "production":
        raise ValueError("Use the production analytics service entry")


def start():
    _local_only()
    if current_pid():
        return
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = STATE / "analytics.log"
    with log.open("a") as output:
        log.chmod(0o600)
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "supervise"],
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    for _ in range(50):
        if current_pid():
            return
        time.sleep(0.1)
    raise RuntimeError(
        "Analytics supervisor did not start; inspect the private analytics log"
    )


def stop():
    if pid := current_pid():
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not current_pid():
                return
            time.sleep(0.1)
        raise RuntimeError("Analytics worker is still stopping")


def supervise():
    _local_only()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "analytics-supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        PID.write_text(str(os.getpid()))
        PID.chmod(0o600)
        stopping = False

        def halt(*_):
            nonlocal stopping
            stopping = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, halt)
        child = None
        try:
            while not stopping:
                if child is None or child.poll() is not None:
                    child = subprocess.Popen(
                        [sys.executable, "-m", "lead_app.analytics_worker"],
                        cwd=ROOT,
                        env={
                            **os.environ,
                            "PYTHONPATH": str(ROOT / "services/api"),
                            "PYTHONUNBUFFERED": "1",
                        },
                        stdin=subprocess.DEVNULL,
                    )
                (STATE / "analytics-heartbeat").write_text(str(time.time()))
                time.sleep(2)
        finally:
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            PID.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status", "supervise"])
    args = parser.parse_args()
    if args.action == "status":
        print("running" if current_pid() else "stopped")
    else:
        globals()[args.action]()
