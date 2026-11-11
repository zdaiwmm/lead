"""Bounded read-only runner DNS evidence; package/test gates stay authoritative."""

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode

HOSTS = (
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "azure.archive.ubuntu.com",
    "packages.microsoft.com",
    "github.com",
)
LIMIT = 12000


def command(argv):
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=15,
            check=False,
        )
        return {
            "exit_code": result.returncode,
            "seconds": round(time.monotonic() - started, 3),
            "stdout": result.stdout[:LIMIT],
            "stderr": result.stderr[:LIMIT],
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "error": type(error).__name__,
            "seconds": round(time.monotonic() - started, 3),
        }


def resolve(host, method):
    if method == "system_getent":
        result = command(["getent", "ahostsv4", host])
    else:
        # Resolve dns.google normally: no fixed IP, resolver change or TLS bypass.
        # Failure to reach this control is evidence too, not permission to retry.
        url = "https://dns.google/resolve?" + urlencode({"name": host, "type": "A"})
        result = command([
            "curl", "--fail", "--silent", "--show-error", "--proto", "=https",
            "--connect-timeout", "5", "--max-time", "12", url,
        ])
        if result.get("exit_code") == 0:
            try:
                reply = json.loads(result.pop("stdout"))
                result["dns"] = {
                    key: reply.get(key) for key in ("Status", "Answer", "Authority")
                }
            except (ValueError, AttributeError):
                result["error"] = "invalid DNS JSON response"
    return {"probe": method, "host": host, **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("before-compose", "before-apt"))
    phase = parser.parse_args().phase

    def emit(value):
        print(json.dumps({"phase": phase, **value}, ensure_ascii=True), flush=True)

    try:
        emit({"probe": "resolv.conf", "contents": Path("/etc/resolv.conf").read_text()[:LIMIT]})
    except OSError as error:
        emit({"probe": "resolv.conf", "error": type(error).__name__})
    # Concurrent, individually bounded probes keep even total DNS loss brief.
    # Print no environment, credentials, application state or proxy settings.
    with ThreadPoolExecutor(max_workers=12) as pool:
        pending = {
            pool.submit(command, argv): {"probe": name}
            for name, argv in (
                ("resolver_status", ["resolvectl", "status", "--no-pager"]),
                ("ipv4_routes", ["ip", "-4", "route", "show"]),
                ("ipv6_routes", ["ip", "-6", "route", "show"]),
            )
        }
        pending.update({
            pool.submit(resolve, host, method): {}
            for host in HOSTS
            for method in ("system_getent", "google_doh")
        })
        for future in as_completed(pending):
            emit({**pending[future], **future.result()})
    # Probe failures are recorded, never substituted for the existing apt gate.


if __name__ == "__main__":
    main()
