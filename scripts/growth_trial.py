"""Manage one Agent's growth pilot from the application's trusted server CLI."""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.budget import configuration
from lead_app.db import audit, emit, j, one, require_agent, tx
from lead_app.identity_store import agent_tenant
from lead_app.scope import tenant_scope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent_id", type=UUID)
    parser.add_argument("mode", choices=("status", "off", "shadow", "active"))
    parser.add_argument("--working-tokens", type=int)
    parser.add_argument("--focus", choices=("first_users", "chamber_first_users", "standard"))
    args = parser.parse_args()
    aid = str(args.agent_id)
    with tenant_scope(agent_tenant(aid)), tx(aid) as c:
        agent = require_agent(c, aid, True)
        old = agent["onboarding_state"].get("growth_trial_policy") or {}
        if (
            args.mode == "status"
            or old.get("mode", "off") == args.mode
            and args.working_tokens is None and args.focus is None
        ):
            print(old or {"mode": "off"})
            return
        limit = (
            args.working_tokens
            if args.working_tokens is not None
            else old.get("working_tokens")
        )
        focus = args.focus or old.get("focus", "standard")
        new_campaign = args.mode == "active" and focus == "chamber_first_users"
        if args.mode != "off" and not new_campaign and (
            not limit or not 0 < limit <= configuration(c, aid)["daily_token_limit"]
        ):
            parser.error(
                "working token allowance must fit within the configured daily limit"
            )
        if args.mode == "active" and old.get("mode") != "active" and not new_campaign:
            compared = old.get("mode") == "shadow" and one(
                c,
                """SELECT id FROM lead.tasks
                WHERE agent_id=%s AND type='operating.decision' AND state='succeeded'
                AND created_at>=%s AND result->'trial_order' ? 'shadow_choice' LIMIT 1""",
                (aid, old["changed_at"]),
            )
            if not compared:
                parser.error(
                    "a completed live shadow decision is required before activation"
                )
        now = datetime.now(timezone.utc).isoformat()
        policy = {
            **old,
            "mode": args.mode,
            "focus": focus,
            "working_tokens": limit,
            "changed_at": now,
            "version": "v1",
        }
        if args.mode == "active" and old.get("mode") != "active":
            policy["activated_at"] = now
        updates = {"growth_trial_policy": policy}
        old_campaign = old.get("mode") == "active" and old.get("focus") == "chamber_first_users"
        if old_campaign != new_campaign:
            updates["community_research_request"] = str(uuid4())
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state||%s WHERE id=%s",
            (j(updates), aid),
        )
        audit(
            c,
            aid,
            "growth_trial.policy",
            {"previous_mode": old.get("mode", "off"), "policy": policy},
        )
        emit(c, aid, "supervisor.wake")
        print(policy)


if __name__ == "__main__":
    main()
