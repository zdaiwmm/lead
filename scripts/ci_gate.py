"""A skipped, failed or cancelled required job cannot become a successful release proof."""

import json
import os


def verify(kind, event, results):
    if kind not in {"application", "tools", "docs"}:
        raise ValueError("Unknown verification kind")
    required = {"scope", "checks"}
    if event == "pull_request":
        if kind == "application":
            required.add("frontend")
    elif kind == "application":
        required |= {"backend", "frontend", "namespace"}
    elif kind == "tools":
        required |= {"delivery", "namespace"}
    if any(v in {"failure", "cancelled"} for v in results.values()):
        raise ValueError("A verification job failed or was cancelled")
    if any(results.get(k) != "success" for k in required):
        raise ValueError("Required verification did not succeed")


if __name__ == "__main__":
    verify(os.environ["SCOPE"], os.environ["EVENT"],
           {k: v["result"] for k, v in json.loads(os.environ["NEEDS_JSON"]).items()})
    print("Required checks verified")
