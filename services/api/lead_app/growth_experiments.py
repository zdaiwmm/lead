"""Agent-scoped acquisition trials. All state transitions run under the agent lock."""

import re
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from .db import all_rows, j, one
from .operating_feedback import enqueue, fingerprint
from .scope import current_tenant

runtime = ContextVar("growth_trial_action", default=None)
TYPE = "growth_experiment_v1"
BUCKETS = {"execution": 70, "exploration": 20, "completion": 10}


def policy(agent):
    return agent["onboarding_state"].get("growth_trial_policy") or {}


def enabled(agent):
    return policy(agent).get("mode") == "active"


def first_users(agent):
    return enabled(agent) and policy(agent).get("focus") in ("first_users", "chamber_first_users")


def chamber_campaign(agent):
    return enabled(agent) and policy(agent).get("focus") == "chamber_first_users"


def bucket(kind):
    if kind in ("operating.opportunities", "operating.strategy", "community_discovery"):
        return "exploration"
    if kind in ("publication_reconcile", "account", "kits"):
        return "completion"
    return "execution"


def ledger(c, start, end):
    rows = all_rows(
        c,
        """SELECT r.pricing_snapshot->'trial'->>'id' AS id,
        r.pricing_snapshot->'trial'->>'bucket' AS bucket, r.pricing_snapshot->'trial'->>'kind' AS kind,
        coalesce(sum(e.units),0)::bigint AS spent,
        coalesce(sum(r.units) FILTER(WHERE r.state='reserved'),0)::bigint AS reserved,
        coalesce(sum(r.units) FILTER(WHERE r.state='unknown'),0)::bigint AS unknown
        FROM lead.budget_reservations r LEFT JOIN lead.usage_events e ON e.reservation_id=r.id
        WHERE r.created_at>=%s AND r.created_at<%s GROUP BY 1,2,3""",
        (start, end),
    )
    return rows


def allowances(c, agent, ceiling, now):
    start = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if first_users(agent):
        return {"daily": None if chamber_campaign(agent) else ceiling}
    limit = min(ceiling, int(policy(agent)["working_tokens"]))
    # After a complete comparable day, trim exploration if its paid work
    # produced no new findings. Leave the execution and receipt shares intact.
    yesterday = one(
        c,
        """SELECT sum(e.units) AS spend FROM lead.usage_events e JOIN lead.budget_reservations r
        ON r.id=e.reservation_id WHERE r.pricing_snapshot#>>'{trial,bucket}'='exploration'
        AND r.created_at>=%s AND r.created_at<%s""",
        (start - timedelta(days=1), start),
    )
    useful = one(
        c,
        """SELECT 1 FROM lead.tasks WHERE input ? 'experiment_id' AND completed_at>=%s AND completed_at<%s
        AND jsonb_array_length(coalesce(result->'new_evidence_keys','[]'::jsonb))>0 LIMIT 1""",
        (start - timedelta(days=1), start),
    )
    rows = ledger(c, start, start + timedelta(days=1))
    result = {key: limit * share // 100 for key, share in BUCKETS.items()}
    activated = policy(agent).get("activated_at")
    if (
        activated
        and datetime.fromisoformat(activated) <= start - timedelta(days=1)
        and yesterday["spend"]
        and not useful
    ):
        result["exploration"] //= 2
    for row in rows:
        if row["bucket"] in result:
            result[row["bucket"]] -= sum(
                row[k] for k in ("spent", "reserved", "unknown")
            )
    return result


def headroom(remaining, category, attributed):
    if "daily" in remaining:
        return float("inf") if remaining["daily"] is None else remaining["daily"]
    room = remaining[category]
    if category == "completion":
        return max(0, min(room, sum(remaining.values())))
    other = "execution" if category == "exploration" else "exploration"
    room += min(0, remaining[other])  # prior debt cannot be lent again
    if attributed:
        # Only a named, evidence-backed hypothesis may cross pools. Lend at
        # most a quarter of unused execution to discovery, never the reserve.
        divisor = 4 if category == "exploration" else 2
        room += max(0, remaining[other]) // divisor
    return max(
        0,
        min(
            room,
            remaining["execution"] + remaining["exploration"],
            sum(remaining.values()),
        ),
    )


def trial_spend(c, trial_id):
    return one(
        c,
        """SELECT coalesce(sum(e.units),0)::bigint AS spent,
        coalesce(sum(r.units) FILTER(WHERE r.state IN ('reserved','unknown')),0)::bigint AS held
        FROM lead.budget_reservations r LEFT JOIN lead.usage_events e ON e.reservation_id=r.id
        WHERE r.pricing_snapshot->'trial'->>'id'=%s""",
        (trial_id,),
    )


def trial_rows(c, business_id, revision, *, active_only=False):
    return all_rows(
        c,
        """SELECT id,body FROM lead.memory_entries WHERE kind='observation'
        AND body->>'type'=%s AND body->>'business_id'=%s AND body->>'revision'=%s
        AND (NOT %s OR body->>'state' IN ('candidate','executing','awaiting_publication','observing'))
        AND deleted_at IS NULL ORDER BY (body->>'state'='routine'),created_at DESC,id LIMIT 100""",
        (TYPE, str(business_id), str(revision), active_only),
    )


def save(c, trial):
    trial["body"]["updated_at"] = datetime.now(timezone.utc).isoformat()
    c.execute(
        "UPDATE lead.memory_entries SET body=%s WHERE id=%s",
        (j(trial["body"]), trial["id"]),
    )


def materialize(c, aid, agent, business, source):
    """Distinct evidence/mechanisms survive repeated decisions and workflow replay."""
    options = (source.get("result") or {}).get("alternatives", [])
    for rank, option in enumerate(options[:2]):
        refs = sorted(set(option.get("evidence_task_ids", [])))
        if not refs or not option.get("mechanism") or not option.get("next_action"):
            continue
        verified = all_rows(
            c,
            """SELECT id FROM lead.tasks WHERE id::text=ANY(%s) AND state='succeeded'
            AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s""",
            (refs, str(business["id"]), str(agent["config_revision"])),
        )
        if len(verified) != len(refs):
            continue
        key = fingerprint(
            [str(business["id"]), agent["config_revision"], option["mechanism"], refs]
            + ([source["result"]["strategy_version"]] if source["result"].get("strategy_version") else [])
        )
        eid = str(uuid5(NAMESPACE_URL, aid + "/acquisition/" + key))
        body = {
            **option,
            "rank": rank,
            "max_tokens": None if first_users(agent) else int(policy(agent)["working_tokens"]) // 3,
            "window_hours": 24,
            "type": TYPE,
            "id": eid,
            "business_id": str(business["id"]),
            "revision": agent["config_revision"],
            "source": str(source["id"]),
            "target_snapshot": source["result"].get("target_snapshot"),
            "strategy_version": (source["result"].get("strategy_version") or str(source["id"])),
            "state": "candidate",
            "executor": option.get("executor") or "public_research",
            "task_ids": [],
            "hosts": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": "有可回读的业务证据，等待比较执行条件。",
        }
        c.execute(
            """INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation)
            VALUES(%s,%s,%s,'observation',%s,%s,'system_observed') ON CONFLICT(id) DO NOTHING""",
            (eid, current_tenant(), aid, j(body), j(refs)),
        )


def first_task(c, aid, agent, business, trial, kind, label, **extra):
    b = trial["body"]
    key = (
        "alternatives:trial:"
        + b["id"]
        + ":"
        + fingerprint([kind, extra.get("candidate_url")])
    )
    tid = enqueue(c, aid, agent, business, kind, label, work_key=key)
    payload = {
        "experiment_id": b["id"],
        "experiment_hypothesis": b["mechanism"],
        "experiment_audience": b["audience"],
        **extra,
    }
    c.execute("UPDATE lead.tasks SET input=input || %s WHERE id=%s", (j(payload), tid))
    b.update(
        state="executing",
        reason=label,
        task_ids=list(dict.fromkeys([*b["task_ids"], tid])),
    )
    save(c, trial)


def link_matches(trial, url, label):
    """Use a source already seen for this question, never the first arbitrary anchor."""
    direct = urlsplit(url)
    explicit = bool(trial.get("entry_url") and trial["entry_url"] == url)
    question = trial["mechanism"] + " " + trial["next_action"]
    named = [
        w.casefold()
        for w in re.findall(r"(?<![A-Za-z])[A-Z][a-zA-Z0-9-]{2,}", question)
        if w.casefold()
        not in {"faq", "the", "find", "check", "verify", "http", "https", "url", "www"}
    ]
    terms = trial.get("source_terms") or named
    description = (url + " " + label).casefold()
    if not explicit and not any(
        term and term.casefold() in description for term in terms
    ):
        return False
    if named and re.search(r"官方|官网|first.party|official", question, re.I):
        # An opaque search redirect may label a third-party guide with the
        # brand name. For a first-party rule require the observed host itself.
        host = (direct.hostname or "").casefold()
        parts = host.split(".")
        return len(parts) >= 2 and parts[-2] in named
    return True


def unused_source(c, b, source_ids):
    from .operating_followups import observed_links

    visited = all_rows(
        c,
        """SELECT input->>'candidate_url' AS url,result->>'blocked_host' AS host,
        (result->>'risk_retry_at')::timestamptz>now() AS cooling FROM lead.tasks
        WHERE type='operating.validate' AND input->>'business_profile_id'=%s
        AND input->>'config_revision'=%s AND created_at>now()-interval '7 days' AND state<>'canceled'""",
        (b["business_id"], str(b["revision"])),
    )
    seen = {r["url"] for r in visited}
    blocked = set(b["hosts"]) | {r["host"] for r in visited if r["cooling"]}
    return next(
        (
            i
            for i in observed_links(c, source_ids)
            if i["url"] not in seen
            and urlsplit(i["url"]).hostname not in blocked
            and link_matches(b, i["url"], i["label"])
        ),
        None,
    )


def validate_source(c, aid, agent, business, trial, item):
    trial["body"]["hosts"].append(urlsplit(item["url"]).hostname)
    first_task(
        c,
        aid,
        agent,
        business,
        trial,
        "validate",
        "核验原始入口：" + item["label"][:100],
        candidate_url=item["url"],
        candidate_label=item["label"],
        observed_in_task_id=item["source_task_id"],
    )


def review_trial(c, aid, agent, business, trial, now):
    b = trial["body"]
    if b["state"] == "observing":
        if now < datetime.fromisoformat(b["review_at"]):
            return
        stats = one(
            c,
            """SELECT count(DISTINCT l.id) AS links,count(DISTINCT v.id) AS clicks
            FROM lead.tasks t JOIN lead.tracking_links l ON l.content_id::text=t.input->>'content_id'
            LEFT JOIN lead.visit_sessions v ON v.tracking_link_id=l.id AND v.exclusion_reason IS NULL
            AND v.trust_level='redirect_observed' AND v.started_at BETWEEN %s AND %s
            WHERE t.input->'operating_plan'->>'experiment_id'=%s""",
            (b["public_at"], b["review_at"], b["id"]),
        )
        prior = all_rows(
            c,
            """WITH history AS (
              SELECT a.task_id,a.confirmed_at,ci.id AS content_id FROM lead.actions a
              JOIN lead.content_items ci ON ci.id=a.content_id JOIN lead.plans p ON p.id=ci.plan_id
              JOIN lead.tasks t ON t.id=a.task_id WHERE a.target_id=%s AND a.state='confirmed'
              AND t.type='telegram.publish' AND coalesce(t.input->>'bootstrap_verification','false')<>'true'
              AND p.business_profile_id=%s AND t.input->>'config_revision'=%s
              AND a.confirmed_at<=%s::timestamptz-interval '24 hours'
              ORDER BY a.confirmed_at DESC LIMIT 3)
              SELECT h.task_id::text AS id,count(DISTINCT v.id) AS clicks FROM history h
              JOIN lead.tracking_links l ON l.content_id=h.content_id
              LEFT JOIN lead.visit_sessions v ON v.tracking_link_id=l.id AND v.exclusion_reason IS NULL
              AND v.trust_level='redirect_observed' AND v.started_at BETWEEN h.confirmed_at AND h.confirmed_at+interval '24 hours'
              GROUP BY h.task_id""",
            (b.get("target_id"), b["business_id"], str(b["revision"]), b["public_at"]),
        )
        from .acquisition_strategy import target_results

        outcomes = {"events": None, "new_users": None} if chamber_campaign(agent) else target_results(c, b.get("target_snapshot"), b["public_at"], b["review_at"], b["id"])
        b.update(
            state="closed",
            comparison=prior,
            target_results=outcomes,
            outcome="observed_target" if outcomes["events"] else "observed_clicks" if stats["clicks"] else "insufficient",
            reason=f"观察窗口结束，{stats['links']} 条短链记录到 {stats['clicks']} 次过滤后点击。",
            **stats,
        )
        save(c, trial)
        return
    if b["state"] == "awaiting_publication":
        post = one(
            c,
            """SELECT t.id,t.type,t.result,t.state,t.input->>'target_id' AS target_id,a.state AS action_state,a.confirmed_at,t.created_at FROM lead.tasks t
            LEFT JOIN lead.actions a ON a.task_id=t.id WHERE t.type LIKE '%%.publish'
            AND t.input->'operating_plan'->>'experiment_id'=%s ORDER BY t.created_at DESC LIMIT 1""",
            (b["id"],),
        )
        from .publication_delivery import is_public

        delivery = (post["result"] or {}).get("delivery") if post else None
        if (
            post
            and post["action_state"] == "confirmed"
            and is_public(post["type"].split(".")[0], delivery)
        ):
            at = (
                datetime.fromisoformat(delivery["public_at"])
                if delivery and delivery.get("public_at")
                else post["confirmed_at"]
            )
            b.update(
                state="observing",
                target_id=post["target_id"],
                public_at=at.isoformat(),
                review_at=(at + timedelta(hours=24)).isoformat(),
                publication_task_id=str(post["id"]),
                reason="发布回执已确认，等待独立短链观察满 24 小时。",
            )
        elif (
            post
            and post["action_state"] not in ("dispatching", "unknown")
            and post["state"] in ("failed", "canceled")
            and now >= datetime.fromisoformat(b["deadline"])
        ):
            b.update(
                state="closed",
                outcome="unavailable",
                reason="原发布未获回执，试验到期。",
            )
        elif post:
            if b.get("publication_task_id") == str(post["id"]):
                return
            b.update(
                publication_task_id=str(post["id"]),
                reason="已有发布任务，等待原任务回执。",
            )
        elif now >= datetime.fromisoformat(b["deadline"]):
            b.update(
                state="closed",
                outcome="unavailable",
                reason="观察期内没有适用的已授权发布窗口，释放试验位置。",
            )
        else:
            return
        save(c, trial)
        return
    if b["state"] != "executing":
        return
    task = one(
        c,
        "SELECT id,type,state,input,result FROM lead.tasks WHERE id=%s",
        (b["task_ids"][-1],),
    )
    if task["type"] == "operating.validate" and not link_matches(
        b, task["input"]["candidate_url"], task["input"].get("candidate_label", "")
    ):
        if task["state"] == "running":
            return
        if task["state"] in ("failed", "planned"):
            c.execute(
                "UPDATE lead.tasks SET state='canceled',completed_at=now(),result=coalesce(result,'{}')||%s WHERE id=%s",
                (j({"reason": "experiment_source_mismatch"}), task["id"]),
            )
        # Reuse a matching observed URL if possible. Otherwise run one
        # hypothesis-specific search; never spend another retry on this link.
        replacement = unused_source(c, b, b["evidence_task_ids"])
        if replacement:
            validate_source(c, aid, agent, business, trial, replacement)
        elif not one(
            c,
            "SELECT 1 FROM lead.tasks WHERE type='operating.opportunities' AND input->>'experiment_id'=%s",
            (b["id"],),
        ):
            first_task(
                c, aid, agent, business, trial, "opportunities", b["next_action"]
            )
        else:
            b.update(
                state="closed",
                outcome="insufficient",
                reason="原链接与试验问题不匹配，已有定向研究未提供匹配入口。",
            )
            save(c, trial)
        return
    result = task["result"] or {}
    if task["state"] not in ("succeeded", "canceled", "failed"):
        return
    if (
        task["state"] == "failed"
        and result.get("attempts", 0) < 2
        and not result.get("blocked_host")
    ):
        return
    if task["type"] == "operating.validate" and result.get("channel_fit") == "viable":
        b.update(
            state="closed",
            outcome="supported",
            reason="已核验入口和参与条件；超出当前账号授权的提交单列。",
        )
    else:
        sources = [t for t in b["task_ids"] if t != str(task["id"])] + b[
            "evidence_task_ids"
        ]
        if task["type"] == "operating.opportunities" and task["state"] == "succeeded":
            sources.insert(0, str(task["id"]))
        item = (
            unused_source(c, b, sources)
            if len(b["hosts"]) < 2 and task["state"] != "canceled"
            else None
        )
        if item:
            validate_source(c, aid, agent, business, trial, item)
            return
        b.update(
            state="closed",
            outcome="insufficient",
            reason=result.get("limitations")
            or result.get("error_code")
            or "可核验来源不足，转向其他打法。",
        )
    save(c, trial)


def advance(c, aid, agent, business, now):
    # The caller holds require_agent(..., True); the proposal and first action
    # are committed by the same transaction as the scheduling decision.
    source = one(
        c,
        """SELECT id,result FROM lead.tasks WHERE type='operating.strategy' AND state='succeeded'
        AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
        AND result->>'status'='hypotheses' ORDER BY completed_at DESC LIMIT 1""",
        (str(business["id"]), str(agent["config_revision"])),
    )
    if source and not (source["result"] or {}).get("trial_options_imported"):
        materialize(c, aid, agent, business, source)
        options = source["result"].get("alternatives", [])
        if options and not any("executor" in o for o in options):
            enqueue(
                c,
                aid,
                agent,
                business,
                "strategy",
                "比较两个机制不同的可执行获客试验，引用近期待核对的业务观察。",
                work_key="alternatives:bootstrap:" + str(source["id"]),
            )
        c.execute(
            "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
            (j({"trial_options_imported": True}), source["id"]),
        )
    rows = trial_rows(c, business["id"], agent["config_revision"], active_only=True)
    for trial in rows:
        if trial["body"]["state"] in ("routine", "closed"):
            continue
        old_state = trial["body"]["state"]
        review_trial(c, aid, agent, business, trial, now)
        b = trial["body"]
        # Only a newly matured channel observation can start a paid replanning
        # pass. Inconclusive research switches to an existing alternative first.
        if old_state == "observing" and b["state"] == "closed":
            tid = enqueue(
                c,
                aid,
                agent,
                business,
                "strategy",
                "依据到期试验决定继续、换受众或停止。",
                work_key="alternatives:feedback:" + b["id"],
            )
            c.execute(
                "UPDATE lead.tasks SET input=input || %s WHERE id=%s",
                (
                    j(
                        {
                            "trial_feedback": {
                                "id": b["id"],
                                "mechanism": b["mechanism"],
                                "reason": b["reason"],
                                "clicks": b.get("clicks"),
                                "comparison": b.get("comparison"),
                                "target_results": b.get("target_results"),
                            }
                        }
                    ),
                    tid,
                ),
            )
            b["next_task_id"] = tid
            save(c, trial)
    active = [
        r["body"]
        for r in rows
        if r["body"]["state"] in ("executing", "awaiting_publication")
    ]
    previous = agent["onboarding_state"].get("operating_kernel", {})
    deferred = {r["key"] for r in previous.get("deferred_candidates", [])}
    occupied = set()
    for b in active:
        ids = b["task_ids"]
        if b["state"] == "executing" and ids:
            lane = previous.get("lanes", {}).get(ids[-1], {})
            retry = one(
                c, "SELECT state,scheduled_at FROM lead.tasks WHERE id=%s", (ids[-1],)
            )
            if (
                ids[-1] in deferred
                or (
                    retry and retry["state"] == "failed" and retry["scheduled_at"] > now
                )
                or (
                    lane.get("next_at")
                    and datetime.fromisoformat(lane["next_at"]) > now
                )
            ):
                continue
        occupied.add(b["executor"])
    if len(active) >= 2:
        return rows

    # A cooled source releases its research slot. Prefer an alternative with
    # a different audience question over another wording of the same idea.
    def affinity(b):
        return {
            w.casefold()
            for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", b["mechanism"])
        }

    recent = [
        r["body"]
        for r in trial_rows(c, business["id"], agent["config_revision"])
        if r["body"]["state"] == "closed" and r["body"].get("outcome") == "insufficient"
    ][:3]
    for trial in sorted(
        rows,
        key=lambda r: (
            any(affinity(r["body"]) & affinity(other) for other in [*active, *recent]),
            r["body"].get("source") != str(source["id"]) if source else True,
            r["body"].get("rank", 0),
            str(r["id"]),
        ),
    ):
        b = trial["body"]
        if b["state"] != "candidate" or b["executor"] in occupied:
            continue
        if any(
            b["executor"] == other["executor"] and affinity(b) & affinity(other)
            for other in active
        ):
            continue
        if any(
            b["executor"] == other["executor"]
            and affinity(b) & affinity(other)
            and set(b["evidence_task_ids"]) == set(other["evidence_task_ids"])
            for other in recent
        ):
            b.update(
                state="closed",
                outcome="already_tested",
                reason="同一来源与假设已有核验结论，等待新的独立证据。",
            )
            save(c, trial)
            continue
        if b["executor"] == "owned_content":
            b.update(
                state="awaiting_publication",
                deadline=(now + timedelta(hours=48)).isoformat(),
                reason="等待已授权自有频道到期，用当前假设规划内容。",
            )
            save(c, trial)
        else:
            item = unused_source(c, b, b["evidence_task_ids"])
            if item:
                validate_source(c, aid, agent, business, trial, item)
            else:
                first_task(
                    c, aid, agent, business, trial, "opportunities", b["next_action"]
                )
        break  # Admit one new hypothesis per pass.
    return rows


def decision_order(choices, previous, now, allowed):
    """Apply fairness only to admitted work. Preserve write fences and due delivery."""
    kept, skipped = [], []
    for x in choices:
        remaining = headroom(allowed, bucket(x.kind), x.experiment_id)
        if x.estimated_token_units and x.estimated_token_units > remaining:
            skipped.append(
                {
                    "kind": x.kind,
                    "key": x.key,
                    "reason": "experiment_working_budget",
                    "estimated_token_units": x.estimated_token_units,
                }
            )
        else:
            kept.append(x)
    last = previous.get("last_kind")
    waiting = previous.get("waiting", {})

    def order(x):
        must = x.kind == "publication_reconcile" or x.delivery_ready or (x.kind == "publish" and "daily" not in allowed)
        since = datetime.fromisoformat(waiting.get(x.key, now.isoformat()))
        age = (now - since).total_seconds()
        rotate = not must and x.kind == last and previous.get("streak", 0) >= 2
        return (not must, rotate, -int(age // 300), -x.priority, x.key)

    kept.sort(key=order)
    selected = kept[0] if kept else None
    state = {
        "last_kind": selected.kind if selected else None,
        "streak": (
            previous.get("streak", 0) + 1 if selected and selected.kind == last else 1
        )
        if selected
        else 0,
        "waiting": {x.key: waiting.get(x.key, now.isoformat()) for x in kept[1:]},
        "runner_up": kept[1].reason if len(kept) > 1 else None,
        "budget_transfer": (
            {
                "to": bucket(selected.kind),
                "for": selected.experiment_id,
                "estimated_gap": selected.estimated_token_units
                - max(0, allowed[bucket(selected.kind)]),
                "reason": "有独立依据的试验借用尚未使用的工作额度，保留回执预留。",
            }
            if selected
            and selected.experiment_id
            and "daily" not in allowed
            and selected.estimated_token_units > max(0, allowed[bucket(selected.kind)])
            else None
        ),
        "wait_cycles": {
            x.key: previous.get("wait_cycles", {}).get(x.key, 0) + 1 for x in kept[1:]
        },
        "selected_reason": selected.reason if selected else None,
    }
    return kept, skipped, state


def report(c, agent, business, now):
    rows = trial_rows(c, business["id"], agent["config_revision"])
    rows = sorted(rows, key=lambda r: r["body"].get("origin") == "existing_work")[:20]
    start = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    costs = ledger(c, start, start + timedelta(days=1))
    for row in rows:
        b = row["body"]
        matched = [v for v in costs if v["id"] == b["id"]]
        lifetime = trial_spend(c, b["id"])
        b.update(lifetime_tokens=lifetime["spent"], lifetime_held=lifetime["held"])
        b["tokens_per_filtered_click"] = (
            lifetime["spent"] / b["clicks"] if b.get("clicks") else None
        )
        outputs = one(
            c,
            """SELECT coalesce(sum(jsonb_array_length(coalesce(t.result->'new_evidence_keys','[]'::jsonb))),0)::int AS evidence,
            count(*) FILTER (WHERE EXISTS (SELECT 1 FROM lead.actions a WHERE a.task_id=t.id AND a.state='confirmed'
                AND(a.external_id IS NOT NULL OR a.external_url IS NOT NULL))) AS confirmed
            FROM lead.tasks t WHERE t.input->>'experiment_id'=%s OR t.input#>>'{operating_plan,experiment_id}'=%s""",
            (b["id"], b["id"]),
        )
        if b.get("executor") == "community_progress":
            community = one(
                c,
                """SELECT count(DISTINCT t.id) AS confirmed, count(DISTINCT v.id) AS clicks
                FROM lead.tasks t LEFT JOIN lead.tracking_links l ON l.id::text=t.input#>>'{reply,tracking_link_id}'
                LEFT JOIN lead.visit_sessions v ON v.tracking_link_id=l.id AND v.trust_level='redirect_observed'
                AND v.exclusion_reason IS NULL AND v.started_at>=%s AND v.started_at<%s
                WHERE t.type='community.engage' AND t.input->>'experiment_id'=%s
                AND t.result->>'reply_state'='confirmed' AND t.input#>>'{reply,promote}'='true'""",
                (start, start + timedelta(days=1), b["id"]),
            )
            outputs["confirmed"] += community["confirmed"]
            b["today_community_clicks"] = community["clicks"]
        b["evidence_count"] = outputs["evidence"]
        b["confirmed_distributions"] = outputs["confirmed"]
        b["tokens_per_finding"] = (
            lifetime["spent"] / outputs["evidence"] if outputs["evidence"] else None
        )
        b["tokens_per_distribution"] = (
            lifetime["spent"] / outputs["confirmed"] if outputs["confirmed"] else None
        )
        b["today_spent"] = sum(v["spent"] for v in matched)
        b["today_held"] = sum(v["reserved"] + v["unknown"] for v in matched)
    from .community_growth import campaign_progress, group_inventory

    return {
        **({"community_window": campaign_progress(c, agent),
            "groups": group_inventory(c, agent["config_revision"])} if first_users(agent) else {}),
        **({"hourly_outreach":agent["onboarding_state"].get("outreach_hour")} if chamber_campaign(agent) else {}),
        "experiments": [r["body"] for r in rows[:20]],
        "costs": costs,
        "working_tokens": None if first_users(agent) else policy(agent)["working_tokens"],
        "focus": policy(agent).get("focus", "standard"),
        "day": start.date().isoformat(),
    }


def community_work_due(c, agent, now):
    from .community_health import eligible_sql
    from .community_runtime import (
        candidate_already_assessed,
        membership_review,
        next_interrupted_read,
        next_voice_edit,
    )
    from .community_schedule import deadlines

    if next_voice_edit(c,agent) or (chamber_campaign(agent) and next_interrupted_read(c,agent["config_revision"])):
        return True
    due = deadlines(c, agent, now)
    if any(at and at <= now for name, at in due.items() if name != "next_discovery_at"):
        return True
    if membership_review(c, focus=first_users(agent)) or one(
        c, """SELECT 1 FROM lead.tasks t WHERE """ + eligible_sql(legacy_receipts=True) + """ AND type='community.engage' AND state='running'
           AND (result->>'receipt_review_after' IS NULL OR (result->>'receipt_review_after')::timestamptz<=now())"""
    ):
        return True
    if chamber_campaign(agent):
        from .community_runtime import next_candidate
        if next_candidate(c, agent["config_revision"], True, chamber=True, proactive=agent["onboarding_state"].get("community_growth", {}).get("allow_proactive_post", False)) is not None:
            return True
        return bool(one(c, """
            SELECT 1 FROM lead.tasks t WHERE """ + eligible_sql() + """ AND type='community.engage' AND state='failed'
              AND input->>'config_revision'=%s AND input#>>'{candidate,selection_basis}'='observe_recent_chat'
              AND input->>'public_handle_recovery' IS NULL AND result->>'error_type'='HTTPStatusError'
              AND result->>'error_detail' LIKE 'TimeoutError: Locator.%%'
              AND NOT (result ?| ARRAY['join_submitted','reply_state']) LIMIT 1
        """, (str(agent["config_revision"]),)))
    rows = all_rows(
        c,
        """SELECT DISTINCT item->>'telegram_url' AS url FROM lead.tasks t,
        LATERAL jsonb_array_elements(t.result->'candidates') item
        WHERE t.type='community.discover' AND t.state='succeeded'
        AND t.input->>'config_revision'=%s AND t.completed_at>now()-interval '7 days'""",
        (str(agent["config_revision"]),),
    )
    return any(not candidate_already_assessed(c, r["url"], focus=first_users(agent)) for r in rows)


def cache_identity(output, prompt, system, reference, version, tools):
    ctx = runtime.get()
    # Only cache read-only structured judgements. Never replay an external
    # action, tool invocation, or generated message from a different question.
    if (
        not ctx
        or tools
        or output.__name__
        not in {
            "CommunityAdmission",
            "ObservationAdmission",
            "ResearchBrief",
            "SourceReport",
            "ChannelCheck",
            "StrategyReport",
            "Discovery",
            "QueryPlan",
            "CatalogSelection",
        }
    ):
        return None
    semantic = output.__name__ in {"CommunityAdmission", "ObservationAdmission", "Discovery", "CatalogSelection"}
    evidence = prompt
    if isinstance(prompt, dict) and isinstance(prompt.get("task"), dict):
        # The UTC clock only affects freshness; use at most the same hour.
        evidence = {**prompt, "task": dict(prompt["task"])}
        if "now_utc" in evidence["task"]:
            if semantic:
                evidence["task"].pop("now_utc")
            else:
                evidence["task"]["now_utc"] = str(evidence["task"]["now_utc"])[:13]
    hour = None if semantic else datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return str(
        uuid5(
            NAMESPACE_URL,
            fingerprint(
                [
                    ctx["aid"],
                    ctx["revision"],
                    ctx["control"],
                    None if semantic else ctx["id"],
                    ctx["kind"],
                    None if semantic else ctx["action"],
                    reference,
                    version,
                    output.model_json_schema(),
                    system,
                    evidence,
                    hour,
                ]
            ),
        )
    )


def cached(aid, key, output):
    from .db import tx

    with tx(aid) as c:
        row = one(
            c,
            """SELECT body->'answer' AS answer FROM lead.memory_entries
            WHERE id=%s AND body->>'type'='trial_judgement' AND expires_at>now()""",
            (key,),
        )
        return output.model_validate(row["answer"]) if row else None


def cache_answer(aid, key, answer):
    from .db import tx

    with tx(aid) as c:
        c.execute(
            """INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation,expires_at)
            VALUES(%s,%s,%s,'observation',%s,'[]','system_observed',now()+make_interval(hours=>%s))
            ON CONFLICT(id) DO UPDATE SET body=excluded.body,expires_at=excluded.expires_at""",
            (
                key,
                current_tenant(),
                aid,
                j(
                    {
                        "type": "trial_judgement",
                        "answer": answer.model_dump(mode="json"),
                    }
                ),
                6 if type(answer).__name__ in {"CommunityAdmission", "ObservationAdmission", "Discovery", "CatalogSelection"} else 1,
            ),
        )


def existing_work(c, aid, agent, business, selected, now):
    """Separate ordinary channel work from model-authored strategy hypotheses."""
    day = now.astimezone(timezone.utc).date().isoformat()
    lane = selected.key if selected.kind == "audience" else selected.target_id or selected.kind
    eid = str(
        uuid5(
            NAMESPACE_URL,
            f"{aid}/{business['id']}/{agent['config_revision']}/{day}/{lane}",
        )
    )
    titles = {
        "community_progress": "在真实问题出现时提供社群帮助",
        "community_discovery": "核验受众实际参与的社群",
        "publish": "在已授权频道验证内容引流",
        "content": "为真实渠道准备内容",
        "operating.strategy": "根据业务证据比较获客打法",
        "operating.opportunities": "核验独立公开分发入口",
    }
    body = {
        "id": eid,
        "type": TYPE,
        "origin": "existing_work",
        "state": "routine",
        "executor": selected.kind,
        "name": titles.get(selected.kind, selected.reason),
        "reason": selected.reason,
        "business_id": str(business["id"]),
        "revision": agent["config_revision"],
        "audience": business.get("facts", {}).get("audience", ""),
        "mechanism": selected.reason,
        "updated_at": now.isoformat(),
        "day": day,
    }
    c.execute(
        """INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation)
        VALUES(%s,%s,%s,'observation',%s,'[]','system_observed') ON CONFLICT(id) DO NOTHING""",
        (eid, current_tenant(), aid, j(body)),
    )
    return eid


def bind_community_task(c, task):
    ctx = runtime.get()
    if not ctx or not ctx.get("id"):
        return
    # A recovered task remains associated with the original trial/day.
    original = task["input"].get("experiment_id")
    if original:
        runtime.set({**ctx, "id": original, "action": str(task["id"])})
    else:
        c.execute(
            "UPDATE lead.tasks SET input=input || %s WHERE id=%s",
            (
                j(
                    {
                        "experiment_id": ctx["id"],
                        "experiment_decision": ctx["decision_id"],
                    }
                ),
                task["id"],
            ),
        )
        runtime.set({**ctx, "action": str(task["id"])})
