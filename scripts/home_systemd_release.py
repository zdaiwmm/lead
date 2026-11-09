#!/usr/bin/python3 -IB
"""Trusted, node-local hook for poll_home_release.py. Install outside release images."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VAULT = Path("/srv/lead-prod-vault")
STORE = VAULT / "lead-git-releases"
TOOLS = Path("/usr/local/lib/lead-home-release")
SHA = re.compile("[a-f0-9]{40}")
BIN = Path("/run/lead-home/bin")
SERVICES = BIN / "original/services"
WEB = BIN / "web"
# Only application processes. Database, network and authentication keep their own units.
ROLES = {"api": SERVICES, "web": WEB, "collector": SERVICES, "worker": SERVICES,
         "browser": SERVICES, "maintenance": SERVICES}


class Refused(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def run(*args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout).stdout.rstrip("\n")


def unit(role):
    return f"lead-home-{role}.service"


def property_of(role, prop):
    return run("systemctl", "show", unit(role), "-p", prop, "--value")


def safe_layout():
    # This is also available to the unprivileged, read-only inventory account.
    for role in ("api", "web", "collector"):
        bindings = property_of(role, "BindReadOnlyPaths") + property_of(role, "DropInPaths")
        require("chamber-" not in bindings.lower(), "shared Chamber application mount")


def image(sha):
    require(SHA.fullmatch(sha), "invalid full SHA")
    p = STORE / sha
    # A root-approved CI image, mounted read-only at its permanent name.
    require(p.resolve(strict=True) == p and p.stat().st_uid == 0, "untrusted release image")
    require(run("findmnt", "-n", "-M", p, "-o", "FSTYPE") == "squashfs", "unexpected release filesystem")
    require(all((p / name).resolve(strict=True) == p / name for name in ("services", "web", "REVISION")),
            "image content is redirected")
    options = run("findmnt", "-n", "-M", p, "-o", "VFS-OPTIONS").split(",")
    require("ro" in options and (p / "REVISION").read_text().strip() == sha, "unsealed release image")
    return p


def process(role):
    pid = property_of(role, "MainPID")
    require(pid.isdecimal() and int(pid) > 1 and property_of(role, "ActiveState") == "active",
            f"{role} process unavailable")
    return Path("/proc") / pid


def bound_tree(proc, target):
    # Kernel mount records, including more specific mounts which could mask the code.
    entries = [line.split() for line in (proc / "mountinfo").read_text().splitlines()]
    nested = [e[4] for e in entries if e[4].startswith(str(target) + "/")]
    cache = str(WEB / ".next/cache")
    require(all(target == WEB and path == cache for path in nested), "unexpected application overlay")
    exact = [e for e in entries if e[4] == str(target)]
    require(len(exact) == 1 and "ro" in exact[0][5].split(","), "unbound or writable application tree")
    return proc / "root" / target.relative_to("/")


def current():
    safe_layout()
    # The selector is only a lookup hint. Every running process must see the immutable image.
    link = STORE / "current"
    require(link.is_symlink(), "no attested live release selector")
    sha = os.readlink(link)
    release = image(sha)
    for role, target in ROLES.items():
        proc = process(role)
        root = bound_tree(proc, target)
        require(root.samefile(release / target.name), f"{role} runs a different image")
        require(process(role) == proc, f"{role} changed during probe")
    return sha


def probe(role, url):
    proc = process(role)
    return run("nsenter", "-t", proc.name, "-n", "--", "curl", "--noproxy", "*",
               "--silent", "--fail", "--max-time", "4", url)


def verify(sha, *, fenced=False):
    require(current() == sha, "running SHA mismatch")
    intake = run("systemctl", "show", "lead-home-ingress.socket", "-p", "ActiveState", "--value")
    require(intake == ("inactive" if fenced else "active"), "unexpected ingress state")
    for role in ("db", "temporal"):
        process(role)
    health = json.loads(probe("api", "http://127.0.0.1:8088/api/health"))
    require(all(health.get(key) is True for key in ("database", "worker", "browser")),
            "API dependencies unavailable")
    if fenced:
        require(health.get("active_profiles") == 0, "browser profiles still in use")
    probe("web", "http://127.0.0.1:3100/")
    # Site-owned read-only check of durable Temporal and the isolated synthetic receipt.
    # Bind its evidence to this worker invocation so an older success cannot be reused.
    invocation = property_of("worker", "InvocationID")
    require(re.fullmatch("[0-9a-f]{32}", invocation), "worker invocation unavailable")
    run(TOOLS / "verify-workflow", sha, invocation)
    require(property_of("worker", "InvocationID") == invocation, "worker changed during verification")
    require(current() == sha, "release changed during verification")


def select(sha):
    with tempfile.TemporaryDirectory(dir=STORE) as scratch:
        tmp = Path(scratch) / "current"
        tmp.symlink_to(sha)
        os.replace(tmp, STORE / "current")
        fd = os.open(STORE, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def restart_and_verify(sha):
    run("systemctl", "restart", *(unit(role) for role in ROLES), timeout=180)
    deadline = time.monotonic() + 90
    while True:
        try:
            verify(sha, fenced=True)
            return
        except (Refused, OSError, ValueError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise Refused("restarted release failed verification") from None
            time.sleep(2)


def replace_release(old, new):
    select(new)
    try:
        restart_and_verify(new)
    except Exception:
        select(old)
        restart_and_verify(old)
        raise Refused("candidate failed; previous release independently verified") from None


def deploy(sha):
    require(os.geteuid() == 0, "trusted node dispatcher required")
    safe_layout()
    require(run("findmnt", "-n", "-M", VAULT, "-o", "TARGET") == str(VAULT), "vault unavailable")
    # Administrative store, including ancestry mirror, images and selector. Never candidate-writable.
    require(STORE.resolve() == STORE and STORE.stat().st_uid == 0 and not STORE.stat().st_mode & 0o022,
            "untrusted release store")
    with (STORE / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = current()
        verify(old)
        if old == sha:
            return
        if not (STORE / sha).is_mount():
            run(TOOLS / "mount-image", sha, timeout=1800)
        image(sha)
        mirror = STORE / "lead.git"
        run("git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", "--git-dir", mirror,
            "fetch", "--no-tags", "https://github.com/zdaiwmm/lead.git",
            "+refs/heads/main:refs/heads/main", timeout=120)
        run("git", "--git-dir", mirror, "merge-base", "--is-ancestor", old, sha)
        run("git", "--git-dir", mirror, "merge-base", "--is-ancestor", sha, "refs/heads/main")
        # Reject schema, dependency, service configuration and runtime changes without a separate plan.
        changes = run("git", "--git-dir", mirror, "diff", "--no-renames", "--name-only", "-z", old, sha)
        allowed = ("services/api/lead_app/", "apps/web/app/", "apps/web/public/", "docs/", "tests/", ".github/")
        require(all(name.startswith(allowed) or name in ("README.md", "AGENTS.md")
                    for name in changes.split("\0") if name), "release requires separate compatibility review")
        # The node's maintenance fence outlives this process. It must remain held across recovery.
        fence = f"lead-home-release-fence@{old}-{sha}.service"
        run("systemctl", "start", fence)
        run(TOOLS / "check-fence", old, sha)
        # Stop both the listener and existing accepted connections. Health probes use loopback.
        # On interruption the independent guard stays up and intake stays closed.
        run("systemctl", "stop", "lead-home-ingress.socket", "lead-home-ingress.service")
        try:
            verify(old, fenced=True)
            run("systemctl", "start", "lead-pc-local-backup.service", timeout=1800)
            run(TOOLS / "check-fence", old, sha)
            require(current() == old, "baseline changed before activation")
            replace_release(old, sha)
        except Exception:
            # Only a positively reverified baseline can take traffic again automatically.
            verify(old, fenced=True)
            run(TOOLS / "check-fence", old, sha)
            run("systemctl", "stop", fence)
            run("systemctl", "start", "lead-home-ingress.socket")
            raise
        run(TOOLS / "check-fence", old, sha)
        run("systemctl", "stop", fence)
        run("systemctl", "start", "lead-home-ingress.socket")
        verify(sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("current", "deploy", "verify"))
    parser.add_argument("sha", nargs="?")
    args = parser.parse_args()
    if (args.action == "current" and args.sha is not None) or (
        args.action != "current" and not SHA.fullmatch(args.sha or "")
    ):
        parser.error("current takes no arguments; deploy and verify require a full SHA")
    try:
        if args.action == "current":
            print(current())
        elif args.action == "verify":
            verify(args.sha)
        else:
            deploy(args.sha)
    except Refused as error:
        sys.exit(str(error))
    except (OSError, ValueError, subprocess.SubprocessError):
        sys.exit("node evidence or operation unavailable; inspect locally")


if __name__ == "__main__":
    main()
