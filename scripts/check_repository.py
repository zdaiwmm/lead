"""Scan tracked/staged content without printing credential values."""

import json
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
secrets = []
config = root / ".state/config.json"
if config.exists():
    values = json.loads(config.read_text())
    secrets += [
        value
        for key, value in values.items()
        if isinstance(value, str)
        and value
        and (key.endswith("_password") or "secret" in key)
    ]

env = root / ".env"
if env.exists():
    from dotenv import dotenv_values

    secrets += [
        v
        for k, v in dotenv_values(env).items()
        if v and any(s in k for s in ("KEY", "PASSWORD", "TOKEN", "SECRET"))
    ]
files = (
    subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
)
issues = []
for name in filter(None, files):
    if (
        name.startswith((".state/", ".venv/"))
        or "/node_modules/" in name
        or name.startswith(".env")
        and name != ".env.example"
    ):
        issues.append(name)
        continue
    path = root / name
    if not path.is_file():
        continue
    data = path.read_bytes()
    if any(s.encode() in data for s in secrets) or re.search(
        rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{24,})", data
    ):
        issues.append(name)
print(
    json.dumps(
        {
            "tracked_files": len(list(filter(None, files))),
            "credential_scan": "passed" if not issues else "failed",
            "flagged_files": issues,
        }
    )
)
raise SystemExit(bool(issues))
