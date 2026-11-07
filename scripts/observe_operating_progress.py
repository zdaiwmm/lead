"""Read-only runtime evidence. Never calls a model or dispatches a publication."""

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
        agent = one(
            c,
            """SELECT desired_state,health_state,heartbeat_at,
          onboarding_state->'operating_kernel'->'last_result' AS last_result,
          octet_length(onboarding_state::text) AS scheduler_bytes FROM lead.agents""",
        )
        usage = one(
            c,
            """SELECT count(*) settled_runs,coalesce(sum(units),0) tokens,
          coalesce(sum((details->>'input_tokens')::bigint),0) input_tokens,
          coalesce(sum((details->>'output_tokens')::bigint),0) output_tokens
          FROM lead.usage_events WHERE category='model_tokens' AND occurred_at>=%s""",
            (since,),
        )
        tasks = all_rows(
            c,
            """SELECT type,state,count(*) n FROM lead.tasks WHERE created_at>=%s
          AND type<>'operating.decision' GROUP BY type,state ORDER BY type,state""",
            (since,),
        )
        publications = all_rows(
            c,
            """SELECT a.state,t.type,
          coalesce(t.input->>'bootstrap_verification','false') AS verification,
          a.external_url FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id
          WHERE a.created_at>=%s ORDER BY a.created_at""",
            (since,),
        )
        return {
            "at": datetime.now(timezone.utc).isoformat(),
            "since": since,
            "agent": agent,
            "usage": usage,
            "tasks": tasks,
            "publications": publications,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--since", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    end = time.monotonic() + args.seconds
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a") as stream:
        while True:
            result = snapshot(args.agent, args.since)
            line = json.dumps(result, default=str, ensure_ascii=False)
            stream.write(line + "\n")
            stream.flush()
            print(line, flush=True)
            if time.monotonic() >= end:
                break
            time.sleep(min(args.interval, max(0, end - time.monotonic())))
