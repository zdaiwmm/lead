"""Evidence-based review produces durable follow-up tasks, even without a channel."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .db import all_rows, j, one, record, uid
from .scope import current_tenant


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()[:24]


def enqueue(
    c,
    aid,
    agent,
    business,
    kind,
    reason,
    *,
    source_review_id=None,
    target_id=None,
    work_key="",
):
    # One open item per source version and work type; reviews attach to that item.
    scope = {
        "business_profile_id": str(business["id"]),
        "config_revision": agent["config_revision"],
        "control_version": agent["control_version"],
    }
    old = one(
        c,
        """SELECT id FROM lead.tasks WHERE type=%s AND (state IN ('planned','running') OR
      (state='failed' AND (coalesce((result->>'attempts')::integer,0)<2 OR completed_at>now()-interval '6 hours')))
      AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
      AND coalesce(input->>'target_id','')=%s AND coalesce(input->>'work_key','')=%s ORDER BY created_at DESC LIMIT 1""",
        (
            "operating." + kind,
            scope["business_profile_id"],
            str(scope["config_revision"]),
            str(target_id or ""),
            work_key,
        ),
    )
    if old:
        if source_review_id:
            c.execute(
                "UPDATE lead.tasks SET input=jsonb_set(input,'{latest_review_id}',%s) WHERE id=%s",
                (j(str(source_review_id)), old["id"]),
            )
        return str(old["id"])
    # Recently completed work remains usable; do not manufacture progress.
    old = one(
        c,
        """SELECT id FROM lead.tasks WHERE type=%s AND state='succeeded' AND input->>'business_profile_id'=%s
      AND input->>'config_revision'=%s AND coalesce(input->>'target_id','')=%s AND coalesce(input->>'work_key','')=%s
      AND completed_at>now()-interval '6 hours' ORDER BY completed_at DESC LIMIT 1""",
        (
            "operating." + kind,
            scope["business_profile_id"],
            str(scope["config_revision"]),
            str(target_id or ""),
            work_key,
        ),
    )
    if old:
        return str(old["id"])
    tid = uid()
    payload = {
        **scope,
        "kind": kind,
        "reason": reason,
        "source_review_id": str(source_review_id) if source_review_id else None,
        "target_id": str(target_id) if target_id else None,
        "mode": "public_read_only",
        "attempts": 0,
        "work_key": work_key,
    }
    c.execute(
        """INSERT INTO lead.tasks(id,tenant_id,agent_id,type,state,workflow_id,input,scheduled_at)
      VALUES(%s,%s,%s,%s,'planned',%s,%s,now())""",
        (
            tid,
            current_tenant(),
            aid,
            "operating." + kind,
            "operating-work:" + tid,
            j(payload),
        ),
    )
    return tid


def review(c, aid, agent, business):
    from .domain import metrics
    from .operating_contract import configuration
    from .operating_playbooks import planning_context

    measured = metrics(c)
    targets = all_rows(
        c,
        "SELECT t.id AS target_id,s.platform FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.state='active' AND s.state='connected'",
    )
    contexts = planning_context(c, targets, business, agent["config_revision"])
    # Existing target-scoped feedback already excludes probes, immature windows,
    # changed objectives/configurations and unconfirmed submissions.
    observations = {
        key: {
            k: v
            for k, v in context.items()
            if k
            in (
                "state",
                "coverage",
                "stats",
                "objective",
                "catalog_version",
                "preferred_plan_id",
                "observation_days",
            )
        }
        for key, context in contexts.items()
    }
    basis = {
        "business": str(business["id"]),
        "revision": agent["config_revision"],
        "metrics": measured,
        "targets": observations,
        "goal": configuration(agent, business),
    }
    key = fingerprint(basis)
    previous = one(
        c,
        "SELECT id,body FROM lead.memory_entries WHERE kind='observation' AND body->>'type'='operating_review' ORDER BY created_at DESC LIMIT 1",
    )
    if previous and previous["body"].get("fingerprint") == key:
        return previous["body"]
    rid = uid()
    successors = []
    contract = basis["goal"]
    aligned = not contract["objective_status"] == "user_defined" or (
        contract.get("objective_event") is not None
        and contract["objective_event"] == measured.get("conversion_objective")
    )
    if (
        measured["coverage"] != "connected"
        or measured["conversion_coverage"] != "connected"
        or not aligned
    ):
        decision, reason = (
            "measurement_required",
            "回传覆盖或经营目标的事件映射尚未完整，效果保持未知；准备具体接入与核对任务。",
        )
        successors.append(
            enqueue(
                c, aid, agent, business, "measurement", reason, source_review_id=rid
            )
        )
    else:
        decision, reason = (
            "observing",
            "保留当前目标口径，等待可比较的成熟观察；其他独立工作继续。",
        )
    for target, observed in observations.items():
        stats = observed.get("stats", [])
        if (
            observed["state"] == "provisional_preference"
            and aligned
            and measured["conversion_coverage"] == "connected"
        ):
            # This remains a provisional operational recommendation, not causal lift.
            successors.append(
                enqueue(
                    c,
                    aid,
                    agent,
                    business,
                    "strategy",
                    "成熟观察支持暂定主方案，准备同目标的后续排期与挑战方案。",
                    source_review_id=rid,
                    target_id=target,
                )
            )
        elif observed.get("coverage") == "connected" and any(
            s.get("mature_posts", 0) for s in stats
        ):
            if not sum(s.get("sessions", 0) for s in stats):
                successors.append(
                    enqueue(
                        c,
                        aid,
                        agent,
                        business,
                        "opportunities",
                        "成熟内容尚无可归因访问，核对新的受众触达入口。",
                        source_review_id=rid,
                        target_id=target,
                    )
                )
            elif (
                aligned
                and measured["conversion_coverage"] == "connected"
                and not sum(s.get("outcomes", 0) for s in stats)
            ):
                successors.append(
                    enqueue(
                        c,
                        aid,
                        agent,
                        business,
                        "journey",
                        "已有成熟访问但尚无目标完成，检查承接；这只是待验证线索。",
                        source_review_id=rid,
                        target_id=target,
                    )
                )
    body = {
        "type": "operating_review",
        "review_id": rid,
        "fingerprint": key,
        "business_profile_id": str(business["id"]),
        "config_revision": agent["config_revision"],
        "goal": basis["goal"],
        "decision": decision,
        "reason": reason,
        "measurement": measured,
        "target_observations": observations,
        "next_task_ids": list(dict.fromkeys(successors)),
        "conclusion": "依据实际覆盖和成熟窗口的运营观察，不推定因果或缺失的业务结果。",
    }
    c.execute(
        "INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation) VALUES(%s,%s,%s,'observation',%s,%s,'system_observed')",
        (rid, current_tenant(), aid, j(body), j([{"metrics_fingerprint": key}])),
    )
    record(c, aid, "已复盘并安排后续任务", reason + f" 关联 {len(successors)} 项工作。")
    return body


def after_task(c, aid, task, result):
    # Persist a continuation before the current cycle returns. A completed
    # preparatory artifact is never interpreted as a lead or publication.
    from .db import require_agent

    agent = require_agent(c, aid, True)
    business = one(
        c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
    )
    kind = task["input"]["kind"]
    follow = {
        "measurement": "journey",
        "journey": "opportunities",
        "opportunities": "strategy",
        "validate": "strategy",
    }.get(kind)
    if follow and result.get("status") != "unchanged":
        tid = enqueue(
            c,
            aid,
            agent,
            business,
            follow,
            "根据已完成的" + kind + "继续验证独立路径。",
            source_review_id=task["input"].get("latest_review_id")
            or task["input"].get("source_review_id"),
            work_key="evidence:" + str(task["id"]) if follow == "strategy" else "",
        )
        result["next_task_id"] = tid
        if follow == "strategy":
            c.execute(
                "UPDATE lead.tasks SET input=input || %s WHERE id=%s",
                (j({"required_evidence_task_id": str(task["id"])}), tid),
            )
    result["next_review_at"] = (
        datetime.now(timezone.utc) + timedelta(hours=6)
    ).isoformat()
    return result


def reconcile_handoffs(c, aid, agent, business, review_id):
    """A successful branch can hand off even when its sibling is cooling down.

    Reconcile committed artifacts after a reload/lost handoff, without repeating
    their model call or mutating their reviewed content.
    """
    completed = all_rows(
        c,
        """SELECT id,input,result FROM lead.tasks WHERE type='operating.journey' AND state='succeeded'
      AND result->>'status'='observed' AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
      AND input->>'control_version'=%s AND completed_at>now()-interval '6 hours' ORDER BY completed_at DESC LIMIT 4""",
        (
            str(business["id"]),
            str(agent["config_revision"]),
            str(agent["control_version"]),
        ),
    )
    for task in completed:
        if task["result"].get("strategy_task_id"):
            continue
        tid = enqueue(
            c,
            aid,
            agent,
            business,
            "strategy",
            "已有实际承接观察，可独立比较替代路径，不等待其他研究分支恢复。",
            source_review_id=review_id,
        )
        c.execute(
            "UPDATE lead.tasks SET input=input || %s WHERE id=%s",
            (j({"evidence_task_id": str(task["id"])}), tid),
        )
        c.execute(
            "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
            (j({"strategy_task_id": tid}), task["id"]),
        )
    from .operating_followups import reconcile

    # A completed research report must not point backwards to a strategy which
    # predates its evidence. Repair only the linkage, never rerun the research.
    stale = all_rows(
        c,
        """SELECT t.id,t.input FROM lead.tasks t LEFT JOIN lead.tasks n ON n.id=(t.result->>'next_task_id')::uuid
      WHERE t.type='operating.opportunities' AND t.state='succeeded' AND t.result->>'status'='observed'
      AND t.input->>'business_profile_id'=%s AND t.input->>'config_revision'=%s AND t.input->>'control_version'=%s
      AND t.completed_at>now()-interval '6 hours' AND (n.id IS NULL OR (n.state='succeeded' AND n.completed_at<t.completed_at))""",
        (
            str(business["id"]),
            str(agent["config_revision"]),
            str(agent["control_version"]),
        ),
    )
    for task in stale:
        tid = enqueue(
            c,
            aid,
            agent,
            business,
            "strategy",
            "用新完成的研究证据重新评估下一步，保留原研究报告。",
            source_review_id=review_id,
            work_key="evidence:" + str(task["id"]),
        )
        c.execute(
            "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
            (j({"next_task_id": tid}), task["id"]),
        )
        c.execute(
            "UPDATE lead.tasks SET input=input || %s WHERE id=%s",
            (j({"required_evidence_task_id": str(task["id"])}), tid),
        )

    reconcile(c, aid, agent, business, review_id)
