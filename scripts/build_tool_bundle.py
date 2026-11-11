"""Package the exact tested Git sources; no application image or working-tree bytes."""

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

from ci_scope import TOOL_FILES


def build(root, sha, output):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])
    if not re.fullmatch("[a-f0-9]{40}", sha) or git("rev-parse", "HEAD").decode().strip() != sha:
        raise ValueError("Exact checked-out commit required")
    subprocess.run(["git", "-C", str(root), "diff", "--quiet", sha, "--", *sorted(TOOL_FILES)], check=True)
    contents, entries = {}, []
    for name in sorted(TOOL_FILES):
        mode = git("ls-tree", sha, "--", name).decode().split()[0]
        if mode not in {"100644", "100755"}:
            raise ValueError("Regular Git source required")
        data = git("show", f"{sha}:{name}")
        if len(data) > 1024 * 1024:
            raise ValueError("Unexpected tool size")
        contents[name] = data
        entries.append({"path": name, "mode": mode, "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {"schema": 1, "sha": sha, "files": entries}
    contents["manifest.json"] = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    with output.open("xb") as file, tarfile.open(fileobj=file, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(contents.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    return manifest


if __name__ == "__main__":
    build(Path.cwd(), sys.argv[1], Path(sys.argv[2]))
