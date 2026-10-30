"""Materialize strategy follow-ups from observed links, never model-invented URLs."""

import ipaddress
import re
from urllib.parse import parse_qs, urlsplit

from .adapters.catalog import SPECS
from .db import all_rows, j
from .operating_feedback import enqueue, fingerprint


def public_candidate(value, label=""):
    if not isinstance(value, str) or len(value) > 3000:
        return None
    try:
        p = urlsplit(value)
    except ValueError:
        return None
    if (
        p.hostname
        and (p.hostname == "google.com" or p.hostname.endswith(".google.com"))
        and p.path == "/url"
    ):
        value = (parse_qs(p.query).get("q") or parse_qs(p.query).get("url") or [""])[0]
        try:
            p = urlsplit(value)
        except ValueError:
            return None
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    host = p.hostname.lower()
    # Search results can expose an opaque Google redirect instead of a final
    # URL. Follow only the observed result link; never infer/decode a destination.
    if (
        host in ("google.com", "www.google.com")
        and p.path == "/goto"
        and parse_qs(p.query).get("url")
        and re.search(r"https?://", label)
    ):
        return p._replace(fragment="").geturl()
    excluded = [
        "google.com",
        "googleusercontent.com",
        "gstatic.com",
        "bing.com",
        "duckduckgo.com",
        "t.me",
        *[s.host.removeprefix("www.") for s in SPECS.values()],
    ]
    if any(host == h or host.endswith("." + h) for h in excluded) or re.search(
        r"(?:^|\.)google\.(?:[a-z]{2}|com\.[a-z]{2}|co\.[a-z]{2})$", host
    ):
        return None
    if re.search(r"/(?:login|logout|register|signup)(?:[/#?]|$)", p.path, re.I):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        if "." not in host:
            return None
    return p._replace(fragment="").geturl()


def observed_links(c, task_ids):
    if not task_ids:
        return []
    rows = all_rows(
        c,
        """SELECT r.task_id,s.output FROM lead.browser_steps s JOIN lead.browser_runs r ON r.id=s.run_id
      JOIN lead.tasks t ON t.id=r.task_id WHERE r.task_id=ANY(%s::uuid[]) AND t.type='operating.opportunities'
      AND t.state='succeeded' AND s.state='completed' AND (t.result->'source_urls') ? (s.output->>'url')
      ORDER BY s.started_at""",
        (task_ids,),
    )
    found = {}
    for row in rows:
        for element in (row["output"] or {}).get("elements", []):
            label = element.get("name", "").strip()
            url = public_candidate(element.get("href"), label)
            if url and label and url not in found:
                found[url] = {
                    "url": url,
                    "label": label[:240],
                    "source_task_id": str(row["task_id"]),
                    "observed_on": row["output"].get("url"),
                }
    return list(found.values())[:24]


def reconcile(c, aid, agent, business, review_id):
    from .operating_admission import assess, context

    admission = context(c, aid, business)
    reports = all_rows(
        c,
        """SELECT id,input,result FROM lead.tasks WHERE type='operating.strategy' AND state='succeeded'
      AND result->>'status'='hypotheses' AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
      AND input->>'control_version'=%s ORDER BY completed_at DESC LIMIT 4""",
        (
            str(business["id"]),
            str(agent["config_revision"]),
            str(agent["control_version"]),
        ),
    )
    completed = all_rows(
        c,
        """SELECT input,result FROM lead.tasks WHERE type LIKE 'operating.%%' AND
      state IN ('succeeded','failed')
      AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s AND completed_at>now()-interval '6 hours'""",
        (str(business["id"]), str(agent["config_revision"])),
    )
    visited = {
        u for row in completed for u in (row["result"] or {}).get("source_urls", [])
    }
    visited.update(row["input"].get("candidate_url") for row in completed)
    for report in reports:
        if report["result"].get("followups_reconciled"):
            next_ids = report["result"].get("next_task_ids", [])
            if not next_ids:
                continue
            linked = all_rows(
                c, "SELECT state FROM lead.tasks WHERE id=ANY(%s::uuid[])", (next_ids,)
            )
            if any(r["state"] in ("planned", "running", "succeeded") for r in linked):
                continue
        ids = list(
            {
                tid
                for a in report["result"].get("alternatives", [])
                for tid in a.get("evidence_task_ids", [])
            }
        )
        candidates = [
            item
            for item in observed_links(c, ids)
            if item["url"] not in visited
            and assess(
                admission,
                "validate",
                78,
                {
                    "input": {
                        "candidate_url": item["url"],
                        "candidate_label": item["label"],
                    }
                },
            )["reason"]
            != "public_host_risk_cooldown"
        ]
        successors = []
        for item in candidates[:2]:
            tid = enqueue(
                c,
                aid,
                agent,
                business,
                "validate",
                "从已观察入口核对受众适配、使用条件与可用分发路径；只读，不提交。",
                source_review_id=review_id,
                work_key=fingerprint(item["url"]),
            )
            c.execute(
                "UPDATE lead.tasks SET input=%s || input WHERE id=%s",
                (
                    j(
                        {
                            "candidate_url": item["url"],
                            "candidate_label": item["label"],
                            "observed_in_task_id": item["source_task_id"],
                            "source_strategy_id": str(report["id"]),
                        }
                    ),
                    tid,
                ),
            )
            successors.append(tid)
        research_followup = not successors and bool(ids)
        if research_followup:
            tid = enqueue(
                c,
                aid,
                agent,
                business,
                "opportunities",
                "已观察入口已核查或受阻；依据新证据寻找不同的需求场景与独立触达入口。",
                source_review_id=review_id,
                work_key="evidence:" + fingerprint(sorted(ids)),
            )
            c.execute(
                "UPDATE lead.tasks SET input=%s || input WHERE id=%s",
                (
                    j(
                        {
                            "source_strategy_id": str(report["id"]),
                            "parent_evidence_ids": sorted(ids),
                            "research_focus": [
                                a.get("next_action", "")
                                for a in report["result"].get("alternatives", [])
                            ],
                        }
                    ),
                    tid,
                ),
            )
            successors.append(tid)
        c.execute(
            "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
            (
                j(
                    {
                        "followups_reconciled": True,
                        "next_task_ids": successors,
                        "followup_status": "scheduled_independent_research"
                        if research_followup
                        else "scheduled_readonly_checks"
                        if successors
                        else "no_unvisited_observed_candidate",
                    }
                ),
                report["id"],
            ),
        )
