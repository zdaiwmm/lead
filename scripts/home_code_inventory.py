#!/usr/bin/python3 -IB
"""Read effective code only. --archive preserves those bytes privately on the mounted vault."""

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BIN = Path("run/lead-home/bin")
APP = BIN / "original/services/api/lead_app"
VAULT = Path("/srv/lead-prod-vault")
SAFE_NAME = re.compile(r"[a-zA-Z0-9_]+\.py")


def read_code(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("unexpected code file type")
        data = f.read()
        after = os.fstat(fd)
        if len(data) != before.st_size or before.st_ctime_ns != after.st_ctime_ns:
            raise ValueError("code changed during scan")
        return data


def add_bytes(bundle, name, data):
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = 0o400
    bundle.addfile(member, io.BytesIO(data))


def scan(unit, proc, bundle, seen):
    root = proc / "root"
    names = {}
    if unit == "lead-home-web.service":
        base = root / BIN / "web/.next"
        files = [base / "BUILD_ID"]
        for folder in ("server", "static"):
            files.extend(p for p in (base / folder).rglob("*")
                         if p.suffix in {".js", ".mjs", ".css", ".wasm"} and not p.is_symlink())
    else:
        base = root / APP
        files = [p for p in base.iterdir() if SAFE_NAME.fullmatch(p.name)]
        if not (base / "api.py").is_file():
            raise ValueError("unknown application layout")
        if unit.startswith("lead-tg-"):
            files += [root / "run/lead-home" / n for n in ("resident.py", "lead_business.py")]
        elif unit == "lead-chamber-analytics.service":
            files.append(root / "run/lead-home/recover_agent_analytics.py")
    for p in sorted(files):
        data = read_code(p)
        key = hashlib.sha256(data).hexdigest()
        names[str(p.relative_to(root))] = key
        if bundle is not None and key not in seen:
            add_bytes(bundle, "objects/" + key, data)
            seen.add(key)
    return names


def processes():
    units = ["lead-home-api.service", "lead-home-collector.service", "lead-home-web.service"]
    listing = subprocess.check_output(["systemctl", "list-units", "--type=service", "--state=running",
                                       "--plain", "--no-legend", "lead-tg-*.service",
                                       "lead-chamber-analytics.service"], text=True)
    units += [line.split()[0] for line in listing.splitlines() if line.split()
              and re.fullmatch(r"lead-(?:tg-[0-9a-f]+|chamber-analytics)\.service", line.split()[0])]
    identities = {}
    for u in units:
        values = subprocess.check_output(["systemctl", "show", u, "-p", "MainPID", "-p", "InvocationID"],
                                         text=True)
        props = dict(line.split("=", 1) for line in values.splitlines())
        if int(props["MainPID"]) <= 1 or not re.fullmatch("[0-9a-f]{32}", props["InvocationID"]):
            raise ValueError("invalid service identity")
        identities[u] = (props["MainPID"], props["InvocationID"])
    return identities


def inventory(bundle=None):
    before = processes()
    records, seen = {}, set()
    for u, (pid, invocation) in before.items():
        proc = Path("/proc", pid)
        names = scan(u, proc, bundle, seen)
        records[u] = {"invocation": invocation, "files": names,
                      "git_entry_present": (proc / "root" / BIN / "original/.git").exists()}
    if processes() != before:
        raise ValueError("service changed during scan")
    return records


def digest_summary(records):
    result = {}
    for unit, record in records.items():
        if unit == "lead-home-web.service":
            digest = hashlib.sha256()
            for name, key in sorted(record["files"].items()):
                rel = Path(name).relative_to(BIN / "web/.next")
                digest.update(os.fsencode(rel) + b"\0" + key.encode() + b"\n")
            result[unit] = {"web_build_digest": digest.hexdigest(), "files_hashed": len(record["files"])}
        else:
            result[unit] = {"code_sha256": {Path(name).name: key for name, key in record["files"].items()},
                            "git_entry_present": record["git_entry_present"]}
    return result


def archive():
    if os.geteuid() or subprocess.check_output(
        ["findmnt", "-n", "-M", VAULT, "-o", "TARGET"], text=True
    ).strip() != str(VAULT):
        raise ValueError("mounted vault and administrator required")
    dest = VAULT / "lead-code-inventory"
    dest.mkdir(mode=0o700, exist_ok=True)
    if dest.resolve() != dest or dest.stat().st_uid or dest.stat().st_mode & 0o077:
        raise ValueError("unsafe vault archive directory")
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        path = Path(tmp, "code.tar")
        with path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o400)
            with tarfile.open(fileobj=stream, mode="w") as bundle:
                manifest = json.dumps(inventory(bundle), sort_keys=True).encode()
                add_bytes(bundle, "manifest.json", manifest)
            stream.flush()
            os.fsync(stream.fileno())
        with path.open("rb") as f:
            checksum = hashlib.file_digest(f, "sha256").hexdigest()
        target = dest / (checksum + ".tar")
        try:
            os.link(path, target)
        except FileExistsError:
            with target.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != checksum:
                    raise ValueError("archive collision")
        fd = os.open(dest, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return checksum


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    try:
        if args.archive:
            print("vault code archive SHA256", archive())
        else:
            print(json.dumps(digest_summary(inventory()), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError):
        sys.exit("code inventory unavailable; check vault, read permission and service identity")
