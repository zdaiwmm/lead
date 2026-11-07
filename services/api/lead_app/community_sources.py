"""Negative evidence is reusable across discovery tasks, not just within a batch."""

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from .db import j, one
from .operating_progress import canonical_url, digest
from .scope import current_tenant


def identity(aid, revision, url, rules):
    return uuid5(NAMESPACE_URL, f"{current_tenant()}:{aid}:community-source:{revision}:{rules}:{canonical_url(url)}")


def cooling(c, aid, revision, url, rules):
    return bool(one(c, "SELECT 1 FROM lead.memory_entries WHERE id=%s AND (body->>'retry_at')::timestamptz>now() AND (body->>'zero_rounds')::int>0",
                    (identity(aid, revision, url, rules),)))


def remember(c, aid, revision, url, rules, evidence, qualified, campaign):
    key = identity(aid, revision, url, rules)
    previous = one(c, "SELECT body FROM lead.memory_entries WHERE id=%s FOR UPDATE", (key,))
    saved = previous["body"] if previous else {}
    fingerprint = digest(evidence)
    # Re-reading identical evidence is not another independent zero-yield round.
    round_key = str(campaign)
    rounds = saved.get("zero_campaigns", []) if not qualified else []
    if not qualified and round_key not in rounds:
        rounds = [*rounds, round_key][-3:]
    delay = 86400 if len(rounds) >= 3 else 3600 if not qualified else 0
    now = datetime.now(timezone.utc)
    body = {"type": "community_source_yield", "url": canonical_url(url), "rules": rules,
            "content_hash": fingerprint, "qualified": qualified, "zero_rounds": len(rounds), "zero_campaigns": rounds,
            "retry_at": (now+timedelta(seconds=delay)).isoformat(), "observed_at": now.isoformat(),
            "next_action": "switch_source" if len(rounds) >= 3 else "revisit_when_due"}
    c.execute("""INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation)
        VALUES(%s,%s,%s,'observation',%s,'[]','system_observed') ON CONFLICT(id) DO UPDATE SET body=excluded.body""",
        (key, current_tenant(), aid, j(body)))
