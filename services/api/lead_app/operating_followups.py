"""Materialize strategy follow-ups from observed links, never model-invented URLs."""

import ipaddress
import re
from urllib.parse import parse_qs, urlsplit

from .adapters.catalog import SPECS
from .db import all_rows, j, one
from .operating_feedback import enqueue, fingerprint


def public_candidate(value, label="", platform=None):
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
    if (
        p.hostname in ("duckduckgo.com", "html.duckduckgo.com")
        and p.path.rstrip("/") == "/l"
    ):
        value = (parse_qs(p.query).get("uddg") or [""])[0]
        try:
            p = urlsplit(value)
        except ValueError:
            return None
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    host = p.hostname.lower()
    if platform:
        spec = SPECS.get(platform)
        if not spec:
            return None
        domain = spec.host.removeprefix("www.")
        if host == domain or host.endswith("." + domain):
            return (
                p._replace(fragment="").geturl()
                if re.fullmatch(spec.target_pattern, p.path)
                else None
            )
        if (
            host in ("google.com", "www.google.com")
            and p.path == "/goto"
            and re.search(
                r"https?://(?:[\w-]+\.)*" + re.escape(domain) + r"(?:/|\s|$)",
                label,
                re.I,
            )
        ):
            return value
        return None
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
    # Footer policies and authentication pages are supporting information,
    # not places where an operator can distribute a product.
    if re.search(r"/(?:privacy[-_]?policy|privacy|terms(?:[-_](?:of[-_])?(?:use|service))?|cookie[-_]?policy)(?:[/.]|$)", p.path, re.I) or re.fullmatch(
        r"(?:privacy policy|terms(?: of (?:use|service))?|cookie policy|隐私政策|隐私声明|隐私条款|用户协议|服务条款)(?:\s*[-|–].*)?", label.strip(), re.I
    ):
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
        """SELECT r.task_id,s.output,t.input->>'platform' AS platform FROM lead.browser_steps s JOIN lead.browser_runs r ON r.id=s.run_id
      JOIN lead.tasks t ON t.id=r.task_id WHERE r.task_id=ANY(%s::uuid[]) AND t.type='operating.opportunities'
      AND t.state='succeeded' AND s.state='completed' AND (t.result->'source_urls') ? (s.output->>'url')
      ORDER BY s.started_at""",
        (task_ids,),
    )
    found = {}
    for row in rows:
        for element in (row["output"] or {}).get("elements", []):
            label = element.get("name", "").strip()
            url = public_candidate(element.get("href"), label, row["platform"])
            if url and label and url not in found:
                found[url] = {
                    "url": url,
                    "label": label[:240],
                    "source_task_id": str(row["task_id"]),
                    "observed_on": row["output"].get("url"),
                }
    return list(found.values())[:24]


def reconcile(c, aid, agent, business, review_id, platform=None, *, alternatives=False):
    from .operating_admission import assess, context

    scope = (str(business["id"]), str(agent["config_revision"]))
    previous = all_rows(
        c,
        """SELECT state,input->>'candidate_url' AS url,input->>'candidate_label' AS label,
        input->>'observed_in_task_id' AS source_id,result->>'channel_fit' AS fit,
        result->>'error_code' AS error_code
        FROM lead.tasks WHERE type='operating.validate' AND state<>'canceled'
        AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s AND (input->>'platform') IS NOT DISTINCT FROM %s
        AND (coalesce(input->>'work_key','') LIKE 'alternatives:%%')=%s""",
        (*scope, platform, alternatives),
    )
    admission = context(c, aid, business)

    def host_available(item):
        return (
            assess(
                admission,
                "validate",
                78,
                {
                    "input": {
                        "candidate_url": item["url"],
                        "candidate_label": item["label"] or "",
                    }
                },
            )["reason"]
            != "public_host_risk_cooldown"
        )

    if any(
        r["state"] in ("planned", "running") and r["url"] and host_available(r)
        for r in previous
    ):
        return True
    visited = {r["url"] for r in previous if r["url"]}
    # Current permissions gate the NEW task. Previously observed public URLs remain
    # evidence after a pause/resume, but not after a business/configuration change.
    sources = all_rows(
        c,
        """SELECT id FROM lead.tasks WHERE type='operating.opportunities'
        AND state='succeeded' AND result->>'status'='observed' AND NOT (input ? 'experiment_id')
        AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
        AND (input->>'platform') IS NOT DISTINCT FROM %s AND (coalesce(input->>'work_key','') LIKE 'alternatives:%%')=%s
        AND completed_at>now()-interval '14 days' ORDER BY completed_at DESC LIMIT 8""",
        (*scope, platform, alternatives),
    )
    for source in sources:
        # Two independently unproductive destinations from the same search are
        # a reason to switch search directions, not to drain its remaining URLs.
        misses = {
            r["url"]
            for r in previous
            if r["source_id"] == str(source["id"])
            and (
                r["fit"] in ("no_entry", "unverified")
                or r["error_code"]
                in (
                    "PUBLIC_ACCESS_CHALLENGE",
                    "PUBLIC_CONTENT_UNAVAILABLE",
                    "NO_VERIFIABLE_PUBLIC_OBSERVATION",
                )
            )
        }
        if len(misses) >= 2:
            continue
        links = observed_links(c, [source["id"]])
        candidates = [
            item
            for item in links
            if item["url"] not in visited and host_available(item)
        ]
        if not candidates:
            continue
        # Prefer a concrete entry point over an informational page; preserve
        # search-result order among equally relevant candidates.
        entry = re.compile(
            r"提交|收录|工具库|目录|资源库|submit|directory|resource library|list your",
            re.I,
        )
        item = max(candidates, key=lambda link: bool(entry.search(link["label"])))
        report = one(
            c,
            """SELECT id FROM lead.tasks WHERE type='operating.strategy' AND state='succeeded'
            AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
            AND EXISTS(SELECT 1 FROM jsonb_array_elements(result->'alternatives') AS alt
                WHERE (alt->'evidence_task_ids') ? %s)
            ORDER BY completed_at DESC LIMIT 1""",
            (*scope, str(source["id"])),
        )
        tid = enqueue(
            c,
            aid,
            agent,
            business,
            "validate",
            "核对公开入口的受众、准入规则与可行下一步："
            + item["label"].splitlines()[0][:100],
            source_review_id=review_id,
            work_key=("alternatives:" if alternatives else "") + fingerprint(item["url"]),
            platform=platform,
        )
        c.execute(
            "UPDATE lead.tasks SET input=input || %s WHERE id=%s",
            (
                j(
                    {
                        "candidate_url": item["url"],
                        "candidate_label": item["label"],
                        "observed_in_task_id": str(source["id"]),
                        **({"source_strategy_id": str(report["id"])} if report else {}),
                    }
                ),
                tid,
            ),
        )
        if report:
            c.execute(
                """UPDATE lead.tasks SET result=result || jsonb_build_object(
                'next_task_ids', coalesce(result->'next_task_ids','[]') || %s,
                'followup_status','scheduled_readonly_checks','followups_reconciled',true)
                WHERE id=%s AND NOT coalesce((result->'next_task_ids') ? %s,false)""",
                (j([tid]), report["id"], tid),
            )
        return True
    return False
