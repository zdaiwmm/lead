"""Retain CI phase duration and status without command output or environment dumps."""

import argparse
import json
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preparation", "cleanup"))
    parser.add_argument("started", type=int)
    parser.add_argument("exitstatus", type=int, nargs="?")
    args = parser.parse_args()
    record = {
        "sha": os.environ["GITHUB_SHA"],
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "shard": int(os.environ["LEAD_CI_SHARD"]),
        "phase": args.phase,
        "seconds": round(max(0, time.time() - args.started), 3),
        "status": os.environ.get("LEAD_CI_JOB_STATUS", "unknown"),
        "exitstatus": args.exitstatus,
    }
    destination = Path(".state/ci-timing.jsonl")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
