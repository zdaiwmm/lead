"""Install the optional exploration runtime without changing app dependencies."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("LEAD_STATE_DIR") or ROOT / ".state").resolve()
RUNTIME = STATE / "browser-use-venv"

if __name__ == "__main__":
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (RUNTIME / "bin/python").exists():
        subprocess.run([sys.executable, "-m", "venv", str(RUNTIME)], check=True)
    subprocess.run(
        [
            str(RUNTIME / "bin/python"),
            "-m",
            "pip",
            "install",
            "-r",
            str(ROOT / "requirements.browser-use.lock.txt"),
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"], check=True
    )
