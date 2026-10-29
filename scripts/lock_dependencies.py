"""Freeze the project environment while retaining OS-specific dependency markers."""

import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
values = subprocess.check_output(
    [sys.executable, "-m", "pip", "freeze"], text=True
).splitlines()
markers = {
    "pyobjc-core": 'sys_platform == "darwin"',
    "pyobjc-framework-cocoa": 'sys_platform == "darwin"',
    "python-xlib": 'sys_platform == "linux"',
}
lines = [
    value
    + (
        " ; " + markers[value.partition("==")[0].lower()]
        if value.partition("==")[0].lower() in markers
        else ""
    )
    for value in values
    if value and not value.startswith(("-e", "lead-local"))
]
(root / "requirements.lock.txt").write_text("\n".join(lines) + "\n")
print("Project dependencies locked with platform markers.")
