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
from contextlib import contextmanager
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
    return role if role.endswith(".service") else f"lead-home-{role}.service"


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


def application_roles():
    if (TOOLS / "home_live_maintenance.py").is_file():
        # These processes also bind current/services at process start. The separately
        # pinned Telegram executor owns another tree and keeps its original scope.
        return ROLES | {"browser-status": SERVICES, "lead-chamber-analytics.service": SERVICES}
    return ROLES


def current():
    safe_layout()
    # The selector is only a lookup hint. Every running process must see the immutable image.
    link = STORE / "current"
    require(link.is_symlink(), "no attested live release selector")
    sha = os.readlink(link)
    release = image(sha)
    for role, target in application_roles().items():
        proc = process(role)
        root = bound_tree(proc, target)
        require(root.samefile(release / target.name), f"{role} runs a different image")
        require(process(role) == proc, f"{role} changed during probe")
    return sha


def probe(role, url):
    proc = process(role)
    return run("nsenter", "-t", proc.name, "-n", "--", "curl", "--noproxy", "*",
               "--silent", "--fail", "--max-time", "4", url)


FRAME_SERVICE = "lead-frame-fast-live.service"


def verify_frames(release, *, running_web=None):
    bundle = release / "home-browser-frames"
    if not bundle.is_dir():
        return  # Earlier release images did not ship the site components.
    require(run("systemctl", "is-enabled", FRAME_SERVICE) == "enabled", "frame service not enabled")
    pid = run("systemctl", "show", FRAME_SERVICE, "-p", "MainPID", "--value")
    require(pid.isdecimal() and int(pid) > 1, "frame service unavailable")
    proc = Path("/proc", pid)
    require(bound_tree(proc, WEB).samefile(running_web or release / "web"), "stale frame dependencies")
    script = proc / "root/run/lead-home/frame.cjs"
    require(script.read_bytes() == (bundle / "frame-live.cjs").read_bytes(), "frame encoder drift")
    unit_file = Path(run("systemctl", "show", FRAME_SERVICE, "-p", "FragmentPath", "--value"))
    require(unit_file.read_bytes() == (bundle / FRAME_SERVICE).read_bytes(), "frame unit drift")
    edge = process("edge") / "root/run/lead-home/nginx.conf"
    site = " ".join(edge.read_text().split())
    for fragment in ("upstream.conf", "location.conf"):
        require(" ".join((bundle / fragment).read_text().split()) in site, "frame route drift")
    require(pid == run("systemctl", "show", FRAME_SERVICE, "-p", "MainPID", "--value"),
            "frame process changed during verification")


def verify(sha, *, fenced=False, require_idle=True):
    require(current() == sha, "running SHA mismatch")
    verify_frames(STORE / sha)
    intake = run("systemctl", "show", "lead-home-ingress.socket", "-p", "ActiveState", "--value")
    require(intake == ("inactive" if fenced else "active"), "unexpected ingress state")
    for role in ("db", "temporal"):
        process(role)
    site = TOOLS / "home_live_maintenance.py"
    health = json.loads(run(site, "health") if site.is_file() else probe("api", "http://127.0.0.1:8088/api/health"))
    require(all(health.get(key) is True for key in ("database", "worker", "browser")),
            "API dependencies unavailable")
    if fenced and require_idle:
        require(health.get("active_profiles") == 0, "browser profiles still in use")
    probe("web", "http://127.0.0.1:3100/")
    # Site-owned read-only check of durable Temporal and the isolated synthetic receipt.
    # Bind its evidence to this worker invocation so an older success cannot be reused.
    invocation = property_of("worker", "InvocationID")
    require(re.fullmatch("[0-9a-f]{32}", invocation), "worker invocation unavailable")
    if site.is_file():
        run(site, "workflow", sha, invocation, timeout=40)
    else:
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
    units = [unit(role) for role in application_roles()]
    # A bind mount follows current at process startup, including when rolling back.
    if run("systemctl", "show", FRAME_SERVICE, "-p", "LoadState", "--value") == "loaded":
        units.append(FRAME_SERVICE)
    run("systemctl", "restart", *units, timeout=180)
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


@contextmanager
def maintenance_window(old, sha):
    """Quiesce the live site without cancelling tasks or reissuing authorizations."""
    site = TOOLS / "home_live_maintenance.py"
    if not site.is_file():
        yield f"lead-home-release-fence@{old}-{sha}.service", [TOOLS / "check-fence"]
        return
    fence = f"lead-home-site-fence@{old}-{sha}.service"
    deadline = time.monotonic() + 3600
    while run(site, "ready", sha) != "idle":
        require(time.monotonic() < deadline, "active work still running; intake untouched")
        time.sleep(15)
    run("systemctl", "stop", "lead-home-live.path")
    try:
        run("systemctl", "stop", "lead-home-ingress.socket", "lead-home-ingress.service")
        run(site, "hold", sha)
        deadline = time.monotonic() + 150
        while json.loads(run(site, "health"))["active_profiles"]:
            require(time.monotonic() < deadline, "existing profiles did not drain")
            time.sleep(3)
        yield fence, [site, "fence"]
    finally:
        # An uncertain switch or an independently held lock needs reconciliation.
        # Never reopen intake or resume scheduling on an unverified revision.
        state = run("systemctl", "show", fence, "-p", "ActiveState", "--value")
        if state in ("inactive", "failed") and current() in (old, sha):
            intake = run("systemctl", "show", "lead-home-ingress.socket", "-p", "ActiveState", "--value")
            revision = current()
            if intake == "active":
                verify(revision)
            else:
                # During a refused/aborted drain a profile may legitimately still be open.
                verify(revision, fenced=True, require_idle=False)
            run(site, "restore", sha)
            run("systemctl", "start", "lead-home-live.path", "lead-home-ingress.socket")


# These node tools were deployed and verified independently of the application image.
# A later app commit may include their historical diff even when every blob is identical.
MAINTENANCE_REVISION = "5684637c5015a3ec1b511aafb5cd39447b90eda9"
MAINTENANCE_FILES = {
    "infra/systemd/lead-home-site-fence@.service", "scripts/home_live_maintenance.py",
    "scripts/home_release_operator.py", "scripts/home_systemd_release.py",
    "scripts/home_verify_workflow.py", "scripts/poll_home_release.py",
}


def reviewed_maintenance_files(mirror, sha, extra):
    paths = extra & MAINTENANCE_FILES
    if not paths or not (TOOLS / "home_live_maintenance.py").is_file():
        return set()
    # Preserve the separate frame compatibility check for other versions of the hook.
    changed = run("git", "--git-dir", mirror, "diff", "--no-renames", "--name-only",
                  "-z", MAINTENANCE_REVISION, sha, "--", *sorted(paths))
    historical = paths - set(changed.split("\0"))
    # These two scripts may already have been upgraded by the node operator.
    # Compare the candidate blob with the independently installed tool bytes.
    for name in (paths - historical) & {"scripts/home_systemd_release.py", "scripts/home_live_maintenance.py"}:
        installed = TOOLS / Path(name).name
        if installed.is_file() and run("git", "hash-object", "--no-filters", installed) == run(
                "git", "--git-dir", mirror, "rev-parse", f"{sha}:{name}"):
            historical.add(name)
    return historical


# Schema changes are applied separately by the node database owner.
CHANNEL_SCHEMA_FILES = {
    ".env.example": "4ea891ba1cdcd4068d80d0ca07cca6460d1d3c01",
    "infra/migrations/versions/0026_first_entry.py": "6c67420e3a578c42181ef45d69bed9ed70ec55bb",
    "infra/migrations/versions/0027_account_profiles.py": "244d0277b597d6f9f6351ff9da7920e8ed098a0c",
    "infra/migrations/versions/0028_distribution_slots.py": "ff2307da5e88017e5d8b91ef7ebcd233f1c77e01",
    "infra/migrations/versions/0029_merge_channel_restore.py": "248855860ddd20d1659f6aef85b443d265fe36b2",
    "infra/migrations/versions/0026_local_restore_proof.py": "0b9554440ea5c15417553d23ae4da8d6520b4377",
}


def reviewed_channel_schema(mirror, sha, extra):
    paths = extra.intersection(CHANNEL_SCHEMA_FILES)
    if not paths or not (TOOLS / "home_live_maintenance.py").is_file():
        return set()
    if any(run("git", "--git-dir", mirror, "rev-parse", f"{sha}:{path}") != CHANNEL_SCHEMA_FILES[path]
           for path in paths):
        return set()
    # The independently installed tool checks the live database, outside the image.
    require(run(TOOLS / "home_live_maintenance.py", "channel-schema") == "ready",
            "channel schema migration has not been verified on this node")
    return paths


# These site units are installed and verified separately; only these reviewed blobs
# are compatible with the home application's release. Future revisions need review.
REDDIT_SITE_FILES = {
    "infra/home-reddit-login/browser.conf": "e80d38a2cb8a020280ba6a2f1cc5f350fe358c59",
    "infra/home-reddit-login/lead-reddit-guard.service": "61031ac0195c3abcc0ae38de9a06ecf3cc688f1d",
    "infra/home-reddit-login/lead-reddit-link.service": "cbeb23de298f2da604630ed2746fbe761b823ac6",
    "infra/home-reddit-login/tokyo-guard.nft": "899f0ca6f4945c69e9ca0039c0da7746d155ff4e",
    "infra/home-reddit-login/tokyo-sshd.conf": "2fb5148544188492b8fab3818f315f5b926ddbaf",
}


def reviewed_reddit_site(mirror, sha, extra):
    paths = extra.intersection(REDDIT_SITE_FILES)
    if not paths or not (TOOLS / "home_live_maintenance.py").is_file():
        return set()
    for path in paths:
        require(run("git", "--git-dir", mirror, "rev-parse", f"{sha}:{path}") == REDDIT_SITE_FILES[path],
                "Reddit site files require separate compatibility review")
    for name, installed in (
        ("browser.conf", "/etc/systemd/system/lead-home-browser.service.d/98-reddit-login.conf"),
        ("lead-reddit-link.service", "/etc/systemd/system/lead-reddit-link.service"),
    ):
        require(run("git", "hash-object", "--no-filters", installed) == REDDIT_SITE_FILES["infra/home-reddit-login/" + name],
                "Reddit browser unit differs from the reviewed version")
    require(run("systemctl", "is-active", "lead-reddit-link.service") == "active", "Reddit tunnel unavailable")
    return paths


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
        extra = {name for name in changes.split("\0") if name and not name.startswith(allowed)
                 and name not in ("README.md", "AGENTS.md")}
        extra -= reviewed_maintenance_files(mirror, sha, extra)
        extra -= reviewed_channel_schema(mirror, sha, extra)
        extra -= reviewed_reddit_site(mirror, sha, extra)
        if extra:
            # The frame subsystem was already installed from this reviewed commit.
            # Only accept its EXACT reviewed blobs, and attest the installed components.
            reviewed = "c8013909ab22a0ea3da15b5bfae7c70443fdca6a"
            frame_files = {"infra/home-browser-frames/" + name for name in
                           ("frame-live.cjs", FRAME_SERVICE, "location.conf", "upstream.conf")}
            compatible = frame_files | {"scripts/build_home_image.sh", "scripts/home_systemd_release.py",
                                        "scripts/request_home_release.py"}
            require(extra <= compatible and (STORE / sha / "home-browser-frames").is_dir(),
                    "release requires separate compatibility review")
            run("git", "--git-dir", mirror, "diff", "--exit-code", reviewed, sha, "--", *sorted(extra))
            verify_frames(image(sha), running_web=image(old) / "web")
        # The node's maintenance fence outlives this process. It must remain held across recovery.
        with maintenance_window(old, sha) as (fence, check):
            run("systemctl", "start", fence)
            run(*check, old, sha)
            # Stop both the listener and existing accepted connections. Health probes use loopback.
            # On interruption the independent guard stays up and intake stays closed.
            run("systemctl", "stop", "lead-home-ingress.socket", "lead-home-ingress.service")
            try:
                verify(old, fenced=True)
                run("systemctl", "start", "lead-pc-local-backup.service", timeout=1800)
                run(*check, old, sha)
                require(current() == old, "baseline changed before activation")
                replace_release(old, sha)
            except Exception:
                # Only a positively reverified baseline can take traffic again automatically.
                verify(old, fenced=True)
                run(*check, old, sha)
                run("systemctl", "stop", fence)
                if not (TOOLS / "home_live_maintenance.py").is_file():
                    run("systemctl", "start", "lead-home-ingress.socket")
                raise
            run(*check, old, sha)
            run("systemctl", "stop", fence)
            if not (TOOLS / "home_live_maintenance.py").is_file():
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
