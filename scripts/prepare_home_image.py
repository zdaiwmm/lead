"""Download a CI-bound SquashFS to private, unprivileged staging on the home node."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

from request_home_release import REPO, api, verified_run

CACHE_LIMIT = 4 * 1024**3


def private_directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Private staging/cache directory required")


def private_file(path):
    """Open owned regular bytes without following a cache/staging symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Private regular staging/cache file required")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def artifact_for(sha, ci, get):
    name = f"lead-home-{sha}-{ci['run_attempt']}"
    listing = get(f"actions/runs/{ci['id']}/artifacts?name={name}&per_page=2")
    matches = [a for a in listing["artifacts"] if a["name"] == name and not a["expired"]]
    if listing["total_count"] != 1 or len(matches) != 1:
        raise ValueError("Unique live build artifact required")
    artifact = matches[0]
    digest = artifact.get("digest") or ""
    if (
        not re.fullmatch("sha256:[a-f0-9]{64}", digest)
        or artifact["workflow_run"]["head_sha"] != sha
        or artifact["workflow_run"]["id"] != ci["id"]
        or type(artifact["id"]) is not int or artifact["id"] <= 0
        or type(artifact["size_in_bytes"]) is not int or artifact["size_in_bytes"] <= 0
    ):
        raise ValueError("Missing exact artifact provenance")
    return artifact


def check_archive(path, artifact):
    with private_file(path) as file:
        size = os.fstat(file.fileno()).st_size
        digest = "sha256:" + hashlib.file_digest(file, "sha256").hexdigest()
    if size != artifact["size_in_bytes"] or digest != artifact["digest"]:
        raise ValueError("Artifact transport size/digest mismatch")


def save_cache(archive, cached, artifact):
    # No eviction. A full cache falls back to the original per-call download.
    size = sum(p.lstat().st_size for p in cached.parent.iterdir())
    if size + artifact["size_in_bytes"] > CACHE_LIMIT:
        return
    with tempfile.TemporaryDirectory(dir=cached.parent) as tmp:
        pending = Path(tmp, "archive.zip")
        with private_file(archive) as reader, pending.open("xb") as writer:
            os.fchmod(writer.fileno(), 0o600)
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        check_archive(pending, artifact)
        pending.chmod(0o400)
        try:
            os.link(pending, cached)
        except FileExistsError:
            # Another preparation may have completed the same authenticated ZIP.
            check_archive(cached, artifact)


def prepare(sha, destination, *, get=api, download_command=("gh",), cache_dir=None,
            archive_only=False):
    ci = verified_run(sha, get=get)
    artifact = artifact_for(sha, ci, get)
    private_directory(destination)
    cached = None
    if cache_dir is not None:
        private_directory(cache_dir)
        cached = cache_dir / f"{artifact['id']}-{artifact['digest'][7:]}.zip"
    target = destination / (f"{sha}-{artifact['id']}.zip" if archive_only else sha + ".squashfs")
    with tempfile.TemporaryDirectory(dir=destination) as tmp:
        archive = Path(tmp, "download.zip")
        with archive.open("xb") as f:
            os.fchmod(f.fileno(), 0o600)
            try:
                reader = private_file(cached) if cached is not None else None
            except FileNotFoundError:
                reader = None
            hit = reader is not None
            if hit:
                with reader:
                    shutil.copyfileobj(reader, f)
            else:
                subprocess.run([*download_command, "api", f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip"],
                               stdout=f, check=True)
        check_archive(archive, artifact)
        image = Path(tmp, "lead.squashfs")
        with zipfile.ZipFile(archive) as z:
            if z.namelist() != ["lead.squashfs"]:
                raise ValueError("Unexpected release archive")
            if not archive_only:
                with z.open("lead.squashfs") as reader, image.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
        if archive_only:
            # Operator workstations need only the authenticated transport ZIP.
            # The receiving node must still extract and attest REVISION before mounting.
            image = archive
        else:
            revision = subprocess.check_output(["unsquashfs", "-cat", image, "REVISION"], text=True)
            if revision != sha + "\n":
                raise ValueError("Image belongs to a different commit")
        image.chmod(0o400)
        with image.open("rb") as f:
            image_digest = hashlib.file_digest(f, "sha256").hexdigest()
        # Neither a cached ZIP nor a successful initial lookup survives a newer
        # failed/restarted CI, expired artifact or changed provenance.
        latest = verified_run(sha, get=get)
        if (latest["id"], latest["run_attempt"]) != (ci["id"], ci["run_attempt"]):
            raise ValueError("CI identity changed during preparation")
        current = artifact_for(sha, latest, get)
        if any(current[key] != artifact[key] for key in ("id", "digest", "size_in_bytes")):
            raise ValueError("Artifact identity changed during preparation")
        if cached is not None and not hit:
            save_cache(archive, cached, artifact)
        try:
            os.link(image, target)
        except FileExistsError:
            with private_file(target) as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != image_digest:
                    raise ValueError("A different image is already staged for this commit") from None
        if archive_only:
            return {"sha": sha, "ci_run": ci["id"], "ci_attempt": ci["run_attempt"],
                    "id": artifact["id"], "name": artifact["name"],
                    "digest": artifact["digest"], "size_in_bytes": artifact["size_in_bytes"],
                    "archive": str(target.resolve()), "revision_verified": False}
        return image_digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha")
    parser.add_argument("private_staging", type=Path)
    parser.add_argument("--cache-dir", type=Path,
                        help="Opt-in private ZIP cache; still verifies live CI and artifact provenance")
    parser.add_argument("--archive-only", action="store_true",
                        help="Fetch a verified transport ZIP on the operator host; node must check REVISION")
    args = parser.parse_args()
    result = prepare(args.sha, args.private_staging, cache_dir=args.cache_dir,
                     archive_only=args.archive_only)
    print(json.dumps(result) if args.archive_only else result)
