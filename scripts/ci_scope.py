"""Fail-closed delivery scope; repository changes, not a PR label, choose verification."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

TOOL_FILES = {
    "scripts/ci_scope.py", "scripts/ci_gate.py", "scripts/build_tool_bundle.py",
    "scripts/tool_release.py", "scripts/tool_install.py",
    "scripts/request_home_release.py", "scripts/poll_home_release.py",
    "scripts/prepare_home_image.py", "scripts/home_release_operator.py",
    "scripts/home_systemd_release.py",
}
TOOL_TESTS = {
    "tests/test_ci_delivery.py", "tests/test_tool_release.py",
    "tests/test_home_release.py", "tests/test_prepare_home_image.py",
    "tests/test_home_systemd_release.py", "tests/home_mount_drill.py",
    "tests/tool_install_drill.py",
}
TOOL_CONFIG = {".github/workflows/ci.yml", ".github/workflows/home-release.yml"}
DOCS = {
    "AGENTS.md", "README.md", "docs/HOME_RELEASE.md", "docs/SMALL_FIX_RELEASE.md",
    "docs/DELIVERY_BACKLOG.md", "docs/requirements/TEMPLATE.md",
}


def is_doc(path):
    return path in DOCS or bool(re.fullmatch(r"docs/requirements/[a-zA-Z0-9_./-]+\.md", path))


def classify(changes):
    """Each change is (path, old mode, new mode). Unknown/mixed inputs run the application."""
    if not changes:
        return "application"
    paths = []
    for path, old, new in changes:
        if path.startswith("/") or ".." in path.split("/") or "\\" in path:
            return "application"
        if old not in {"000000", "100644", "100755"} or new not in {"000000", "100644", "100755"}:
            return "application"
        if not is_doc(path) and new == "000000":
            return "application"
        paths.append(path)
    if all(is_doc(p) for p in paths):
        return "docs"
    if all(is_doc(p) or p in TOOL_FILES | TOOL_TESTS | TOOL_CONFIG for p in paths):
        return "tools"
    return "application"


def changed_files(root, base, head, *, merge_base=False):
    if not all(re.fullmatch(r"[a-f0-9]{40}", x or "") and x != "0" * 40 for x in (base, head)):
        raise ValueError("Exact available history required")
    if merge_base:
        base = subprocess.check_output(["git", "merge-base", base, head], cwd=root, text=True).strip()
    raw = subprocess.check_output(
        ["git", "diff", "--raw", "--no-abbrev", "--no-renames", "-z", base, head], cwd=root
    ).decode().split("\0")
    changes = []
    for i in range(0, len(raw) - 1, 2):
        meta, path = raw[i:i + 2]
        fields = meta.removeprefix(":").split()
        if len(fields) != 5:
            raise ValueError("Incomplete Git diff")
        changes.append((path, fields[0], fields[1]))
    return changes


def check_docs(root, changes):
    root = root.resolve()
    for name, _, mode in changes:
        if not is_doc(name) or mode not in {"100644", "100755"}:
            continue
        path = root / name
        for href in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            href = href.strip("<>").split("#", 1)[0]
            if not href or re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", href):
                continue
            target = (path.parent / href).resolve()
            if not target.is_relative_to(root) or not target.exists():
                raise ValueError(f"Broken/outside repository documentation link in {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    name = os.environ["GITHUB_EVENT_NAME"]
    try:
        if name == "pull_request":
            base, head = event["pull_request"]["base"]["sha"], event["pull_request"]["head"]["sha"]
        elif name == "push":
            base, head = event["before"], os.environ["GITHUB_SHA"]
        else:
            raise ValueError("Manual/unknown event uses full application verification")
        changes = changed_files(args.root, base, head, merge_base=name == "pull_request")
        kind = classify(changes)
        tools = any(p in TOOL_FILES | TOOL_CONFIG | TOOL_TESTS for p, _, _ in changes)
    except (KeyError, ValueError, subprocess.SubprocessError, UnicodeError):
        kind = "application"
        tools = True
    else:
        check_docs(args.root, changes)
    print(json.dumps({"kind": kind, "event": name, "tools": tools}))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as f:
        f.write(f"kind={kind}\n")
        f.write(f"tools={str(tools).lower()}\n")


if __name__ == "__main__":
    main()
