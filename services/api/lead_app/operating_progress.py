"""Progress is new evidence or an executable outcome, never a new task ID."""

import hashlib
import json
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .db import all_rows, j, uid
from .scope import current_tenant

VERSION = 1


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def canonical_url(value):
    p = urlsplit(value)
    query = sorted(
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    )
    return urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(query), "")
    )


def scope(task):
    return digest(
        [task["input"].get("business_profile_id"), task["input"].get("config_revision")]
    )


def page_keys(observations):
    return [
        digest([canonical_url(o["url"]), normalize(o["text"])]) for o in observations
    ]


def known(c, task, keys):
    if not keys:
        return set()
    return {
        r["key"]
        for r in all_rows(
            c,
            """SELECT body->>'key' AS key FROM lead.memory_entries
      WHERE kind='observation' AND body->>'type'='operating_evidence'
      AND body->>'scope'=%s AND body->>'key'=ANY(%s)""",
            (scope(task), keys),
        )
    }


def remember(c, aid, task, key, url, category):
    if key in known(c, task, [key]):
        return False
    c.execute(
        """INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation)
      VALUES(%s,%s,%s,'observation',%s,%s,'system_observed')""",
        (
            uid(),
            current_tenant(),
            aid,
            j(
                {
                    "type": "operating_evidence",
                    "scope": scope(task),
                    "key": key,
                    "url": canonical_url(url),
                    "category": category,
                }
            ),
            j([{"task_id": str(task["id"])}]),
        ),
    )
    return True


def finding_key(finding):
    return digest([canonical_url(finding["source_url"]), normalize(finding["quote"])])


def accept(c, aid, task, result):
    """Called under the agent lock: retries and concurrent cycles cannot mint progress."""
    keys = []
    for finding in result.get("findings", []):
        key = finding_key(finding)
        if remember(c, aid, task, key, finding["source_url"], "finding"):
            keys.append(key)
    for page in result.pop("observed_pages", []):
        remember(c, aid, task, page["key"], page["url"], "page")
    if result.get("status") == "observed":
        result["new_evidence_keys"] = keys
        result["progress"] = "new_evidence" if keys else "no_new_evidence"
        if not keys:
            result["status"] = "unchanged"
    return result


def environment_key(c, agent, business):
    from .domain import metrics

    # No clock, task UUIDs, heartbeat or model prose in the wake condition.
    channels = all_rows(
        c,
        """SELECT s.id,s.external_account_id,s.state,s.automation_enabled,
      t.id AS target,t.state AS target_state,t.verified_permissions,
      g.id AS grant,g.revoked_at FROM lead.social_accounts s
      LEFT JOIN lead.account_targets t ON t.account_id=s.id
      LEFT JOIN lead.authorization_grants g ON g.target_id=t.id ORDER BY s.id,t.id,g.id""",
    )
    m = metrics(c)
    return digest(
        [
            str(business["id"]),
            agent["config_revision"],
            channels,
            {
                k: m[k]
                for k in (
                    "coverage",
                    "conversion_coverage",
                    "published",
                    "visits",
                    "conversions",
                )
            },
        ]
    )


def compact_work(rows):
    result = []
    for row in rows:
        report = row["result"] or {}
        result.append(
            {
                "id": str(row["id"]),
                "input": {"kind": row["input"].get("kind")},
                "result": {
                    k: report[k]
                    for k in (
                        "title",
                        "summary",
                        "status",
                        "findings",
                        "missing",
                        "limitations",
                    )
                    if k in report
                },
            }
        )
    return result


def seed_history(c, aid, agent, business):
    task = {
        "input": {
            "business_profile_id": str(business["id"]),
            "config_revision": agent["config_revision"],
        }
    }
    marker = digest(["history_seed", scope(task), VERSION])
    if marker in known(c, task, [marker]):
        return
    rows = all_rows(
        c,
        """SELECT id,result FROM lead.tasks WHERE state='succeeded'
      AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
      AND result ? 'findings' ORDER BY completed_at""",
        (str(business["id"]), str(agent["config_revision"])),
    )
    for row in rows:
        for finding in row["result"].get("findings", []):
            if finding.get("source_url") and finding.get("quote"):
                remember(
                    c,
                    aid,
                    {**task, "id": row["id"]},
                    finding_key(finding),
                    finding["source_url"],
                    "finding",
                )
    remember(
        c,
        aid,
        {**task, "id": aid},
        marker,
        business.get("product_url") or "https://invalid.local/",
        "seed",
    )
