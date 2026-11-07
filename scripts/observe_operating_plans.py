"""Observe product-owned execution without waking it or spending model tokens."""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.db import all_rows, one, tx


def snapshot(aid, since):
    with tx(aid) as c:
        c.execute("SET TRANSACTION READ ONLY")
        return {
            "at": datetime.now(timezone.utc).isoformat(),
            "since": since,
            "agent": one(
                c,
                "SELECT desired_state,heartbeat_at,onboarding_state->'operating_kernel' AS kernel FROM lead.agents",
            ),
            "consumption": one(
                c,
                "SELECT count(*) settled_calls,coalesce(sum(units),0) tokens FROM lead.usage_events WHERE category='model_tokens' AND occurred_at>=%s",
                (since,),
            ),
            "plans": all_rows(
                c,
                "SELECT id,body->>'horizon' horizon,body->>'period' period,body->>'version' version,body->>'mode' mode FROM lead.memory_entries WHERE body->>'type'='operating_plan' AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 4",
            ),
            "decisions": one(
                c,
                "SELECT count(*) n,count(*) FILTER(WHERE result->>'daily_plan_id' IS NOT NULL) linked_to_plan FROM lead.tasks WHERE type='operating.decision' AND created_at>=%s",
                (since,),
            ),
            "new_work": all_rows(
                c,
                "SELECT type,state,count(*) n FROM lead.tasks WHERE type<>'operating.decision' AND created_at>=%s GROUP BY type,state",
                (since,),
            ),
            "publications": all_rows(
                c,
                "SELECT a.state,a.external_url,t.type,t.input->>'bootstrap_verification' AS verification FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id WHERE a.created_at>=%s",
                (since,),
            ),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.seconds < 0 or args.interval < 1:
        parser.error("seconds must be nonnegative and interval must be positive")
    since = datetime.now(timezone.utc).isoformat()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    end = time.monotonic() + args.seconds
    with output.open("w") as stream:
        while True:
            result = snapshot(args.agent, since)
            stream.write(json.dumps(result, default=str, ensure_ascii=False) + "\n")
            stream.flush()
            if time.monotonic() >= end:
                print(
                    json.dumps(
                        {
                            k: result[k]
                            for k in (
                                "at",
                                "consumption",
                                "decisions",
                                "new_work",
                                "publications",
                            )
                        },
                        default=str,
                    )
                )
                break
            time.sleep(min(args.interval, max(0, end - time.monotonic())))
