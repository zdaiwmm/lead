"""Versioned upgrades; runtime never owns or migrates the schema."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.migrations import upgrade

if __name__ == "__main__":
    upgrade()
    print("Versioned database upgrades and scoped runtime grants ready.")
