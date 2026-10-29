"""Explicit operator recovery. No customer database is selected implicitly."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app import recovery


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "action",
        choices=["prepare", "isolate-source", "install-profile", "review", "release"],
    )
    p.add_argument("--target", required=True)
    p.add_argument("--source")
    p.add_argument("--node-root")
    p.add_argument("--case")
    p.add_argument("--quarantine")
    p.add_argument("--target-id")
    p.add_argument("--note")
    a = p.parse_args()
    if a.action == "prepare":
        r = recovery.prepare(a.target, a.source, a.node_root)
    elif a.action == "isolate-source":
        r = recovery.isolate_source(a.target, a.case)
    elif a.action == "install-profile":
        r = recovery.install_profile(a.target, a.case, a.quarantine)
    elif a.action == "review":
        r = recovery.review(a.target, a.case, a.target_id, a.note or "")
    else:
        r = asyncio.run(recovery.release(a.target, a.case))
    print(json.dumps(r, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": str(error)
                    if isinstance(error, ValueError)
                    else type(error).__name__
                }
            )
        )
        raise SystemExit(1) from None
