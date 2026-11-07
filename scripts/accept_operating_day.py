"""Start/resume or inspect a passive, product-owned 24 hour acceptance run."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.acceptance import create, status

parser = argparse.ArgumentParser(description=__doc__)
group = parser.add_mutually_exclusive_group(required=True)
group.add_argument("--start", metavar="AGENT_ID")
group.add_argument("--status", metavar="RUN_ID")
args = parser.parse_args()
result = create(args.start) if args.start else status(args.status)
if args.status:
    sample = result["latest"] or {}
    result = {
        "run": result["run"],
        "usage": sample.get("usage"),
        "decisions": sample.get("decisions"),
        "publications": sample.get("publications"),
        "models": sample.get("models"),
        "kernel": sample.get("kernel"),
        "result": result["result"],
    }
print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
