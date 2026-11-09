"""Apply the acquisition fix to the four running home services and verify their views."""
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from base_install import AID, APP, LIVE, UNITS, V, VAULT, health, install_file, run, unit_file

ANALYTICS = "lead-chamber-analytics"
API = "lead-home-api"
WEB = "lead-home-web"
COLLECTOR = "lead-home-collector"
MODULES = {
    ANALYTICS: ("analytics_reporting.py", "pixel_events.py"),
    COLLECTOR: ("pixel.js", "pixel_events.py"),
    API: ("analytics_reporting.py", "pixel_events.py", "domain.py", "pixel.js"),
    WEB: (),
}


def running_file(service, target):
    pid = int(run("systemctl", "show", "-p", "MainPID", "--value", service))
    if pid <= 1:
        raise RuntimeError(service + " is not running")
    return Path(f"/proc/{pid}/root") / target.lstrip("/")


def restart():
    run("systemctl", "daemon-reload")
    for service in MODULES:
        run("systemctl", "restart", service)
        run("systemctl", "is-active", service)
    health(API, 8088, "/api/agents", "401")
    health(COLLECTOR, 18190, "/lead.js", "200")
    health(WEB, 3100, "/", "200")


def probe(base, release, bindings):
    name = "lead-attribution-check.service"
    path = Path("/run/systemd/system") / name
    unit = unit_file(base).replace("Restart=on-failure\nRestartSec=15", "Type=oneshot")
    start = f"ExecStart=/usr/bin/env {V}/bin/py/bin/python3.12 -B -u {V}/recover_agent_analytics.py --agent {AID}"
    if start not in unit:
        raise RuntimeError("Unrecognized analytics unit")
    setup = (bindings.removeprefix("[Service]\n") +
             f"BindReadOnlyPaths={release}/attribution_probe.py:{V}/attribution_probe.py\n" +
             f"ExecStart={V}/bin/py/bin/python3.12 -B {V}/attribution_probe.py {AID}")
    try:
        install_file(path, unit.replace(start, setup).encode())
        run("systemctl", "daemon-reload")
        run("systemctl", "start", name)
        print(run("journalctl", "-u", name, "-g", "^Attribution verified", "-n", "1", "-o", "cat"), flush=True)
    finally:
        path.unlink(missing_ok=True)
        run("systemctl", "daemon-reload")


def loaded(release, build):
    if running_file(WEB, V + "/bin/web/.next/BUILD_ID").read_text().strip() != build:
        return False
    return all(
        running_file(service, f"{V}/bin/{APP}/{name}").read_bytes() == (release/name).read_bytes()
        for service, names in MODULES.items() for name in names
    )


def main(mode):
    if os.geteuid() or run("findmnt", "-nr", "-M", str(VAULT), "-o", "UUID") != "16a81cc6-d008-4bfc-acb5-2aa917c06dc9":
        raise RuntimeError("Mounted home production vault and administrator required")
    manifest = json.loads((ROOT / "manifest.json").read_text())
    build = manifest["build"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", build):
        raise ValueError("Invalid web build")
    base = LIVE / "chamber-workspace-releases" / "DEFiCMcFpyUonQuDswDoK"
    release = LIVE / ("attribution-" + build)
    configs = {}
    for service, names in MODULES.items():
        binds = [f"BindReadOnlyPaths={release}/{n}:{V}/bin/{APP}/{n}" for n in names]
        if service == WEB:
            binds.append(f"BindReadOnlyPaths={release}/web:{V}/bin/web/.next")
        configs[service] = "[Service]\n" + "\n".join(binds) + "\n"
    drops = {s: UNITS / (s + ".service.d") / "97-promotion-attribution.conf" for s in MODULES}
    for service, path in drops.items():
        if path.exists() and path.read_text() != configs[service]:
            raise RuntimeError("Another attribution release owns " + str(path))
    if mode == "rollback":
        if not any(p.exists() for p in drops.values()):
            return
        for p in drops.values():
            p.unlink(missing_ok=True)
        restart()
        return
    if all(p.exists() for p in drops.values()):
        if not loaded(release, build):
            restart()
        if not loaded(release, build):
            raise RuntimeError("Running attribution release does not match its package")
        probe(base, release, configs[ANALYTICS])
        return
    if any(p.exists() for p in drops.values()):
        raise RuntimeError("Partial activation detected. Run rollback before retrying.")
    if running_file(WEB, V+"/bin/web/.next/BUILD_ID").read_text().strip() != base.name:
        raise RuntimeError("The running workspace changed. Reconcile its build before deployment.")
    for service, names in MODULES.items():
        for name in names:
            sha = hashlib.sha256(running_file(service, f"{V}/bin/{APP}/{name}").read_bytes()).hexdigest()
            if sha not in manifest["attribution_baselines"][name]:
                raise RuntimeError("Unexpected running source: " + service + "/" + name)
    release.mkdir(mode=0o755, exist_ok=True)
    release.chmod(0o755)
    for name in {n for names in MODULES.values() for n in names}:
        install_file(release / name, (ROOT / "app" / name).read_bytes())
    install_file(release / "attribution_probe.py", (ROOT / "attribution_probe.py").read_bytes())
    web = release / "web"
    shutil.copytree(ROOT / "web", web, dirs_exist_ok=True)
    # Also serve immutable chunks requested by tabs opened on the previous build.
    for src in (base / ".next/static").rglob("*"):
        dst = web / "static" / src.relative_to(base / ".next/static")
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    for p in [web, *web.rglob("*")]:
        p.chmod(0o755 if p.is_dir() else 0o644)
    try:
        for service, path in drops.items():
            install_file(path, configs[service].encode())
        restart()
        if not loaded(release, build):
            raise RuntimeError("Running attribution release does not match its package")
        probe(base, release, configs[ANALYTICS])
    except BaseException:
        for path in drops.values():
            path.unlink(missing_ok=True)
        restart()
        raise


if __name__ == "__main__":
    main(sys.argv[1])
