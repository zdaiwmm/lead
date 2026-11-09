"""Download a CI-bound SquashFS to private, unprivileged staging on the home node."""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from request_home_release import REPO, api, verified_run


def prepare(sha, destination):
    ci = verified_run(sha)
    name = f"lead-home-{sha}-{ci['run_attempt']}"
    listing = api(f"actions/runs/{ci['id']}/artifacts?name={name}&per_page=2")
    matches = [a for a in listing["artifacts"] if a["name"] == name and not a["expired"]]
    if listing["total_count"] != 1 or len(matches) != 1:
        raise ValueError("Unique live build artifact required")
    artifact = matches[0]
    digest = artifact.get("digest") or ""
    if not re.fullmatch("sha256:[a-f0-9]{64}", digest) or artifact["workflow_run"]["head_sha"] != sha:
        raise ValueError("Missing exact artifact provenance")
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    if destination.stat().st_uid != os.getuid() or destination.stat().st_mode & 0o077:
        raise ValueError("Private staging directory required")
    target = destination / (sha + ".squashfs")
    with tempfile.TemporaryDirectory(dir=destination) as tmp:
        archive = Path(tmp, "download.zip")
        with archive.open("xb") as f:
            subprocess.run(["gh", "api", f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip"],
                           stdout=f, check=True)
        with archive.open("rb") as f:
            if "sha256:" + hashlib.file_digest(f, "sha256").hexdigest() != digest:
                raise ValueError("Artifact transport digest mismatch")
        image = Path(tmp, "lead.squashfs")
        with zipfile.ZipFile(archive) as z:
            if z.namelist() != ["lead.squashfs"]:
                raise ValueError("Unexpected release archive")
            with z.open("lead.squashfs") as reader, image.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
        revision = subprocess.check_output(["unsquashfs", "-cat", image, "REVISION"], text=True)
        if revision != sha + "\n":
            raise ValueError("Image belongs to a different commit")
        image.chmod(0o400)
        with image.open("rb") as f:
            image_digest = hashlib.file_digest(f, "sha256").hexdigest()
        try:
            os.link(image, target)
        except FileExistsError:
            with target.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != image_digest:
                    raise ValueError("A different image is already staged for this commit") from None
        return image_digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha")
    parser.add_argument("private_staging", type=Path)
    args = parser.parse_args()
    print(prepare(args.sha, args.private_staging))
