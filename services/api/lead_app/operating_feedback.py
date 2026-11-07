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
    platform=None,
):
    # One open item per source version and work type; reviews attach to that item.
    scope = {
        "business_profile_id": str(business["id"]),
        "config_revision": agent["config_revision"],
        "control_version": agent["control_version"],
        "progress_version": 1,
    }
    if kind in ("journey", "opportunities") and not work_key:
        from .operating_progress import environment_key

        work_key = "environment:" + environment_key(c, agent, business)
    if platform:
        scope["platform"] = platform
        work_key = f"{platform}:{work_key}"
    old = one(
        c,
        """SELECT id FROM lead.tasks WHERE type=%s AND (state IN ('planned','running') OR
      (state='failed' AND (coalesce((result->>'attempts')::integer,0)<2 OR completed_at>now()-interval '6 hours')))
      AND input->>'progress_version'='1' AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
      AND input->>'control_version'=%s AND coalesce(input->>'target_id','')=%s AND coalesce(input->>'work_key','')=%s ORDER BY created_at DESC LIMIT 1""",
        (
            "operating." + kind,
            scope["business_profile_id"],
            str(scope["config_revision"]),
            str(scope["control_version"]),
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
      AND input->>'progress_version'='1' AND input->>'config_revision'=%s AND coalesce(input->>'target_id','')=%s AND coalesce(input->>'work_key','')=%s
      AND (completed_at>now()-interval '6 hours' OR type NOT IN ('operating.journey','operating.opportunities')) ORDER BY completed_at DESC LIMIT 1""",
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
    coalesce_environment_work(c, scope)
    return tid


def coalesce_environment_work(c, scope):
    """Supersede only unstarted reviews of the same target/environment question."""
    c.execute(
        """WITH pending AS (
          SELECT id,row_number() OVER(PARTITION BY type,coalesce(input->>'target_id','')
            ORDER BY created_at DESC,id DESC) AS position
          FROM lead.tasks WHERE state='planned' AND type IN ('operating.journey','operating.opportunities')
            AND input->>'progress_version'='1' AND input->>'business_profile_id'=%s
            AND input->>'config_revision'=%s AND input->>'work_key' LIKE 'environment:%%')
        UPDATE lead.tasks t SET state='canceled',completed_at=now(),result=coalesce(result,'{}'::jsonb) || %s
        FROM pending p WHERE t.id=p.id AND p.position>1""",
        (
            scope["business_profile_id"],
            str(scope["config_revision"]),
            j({"reason": "superseded_environment", "not_a_business_outcome": True}),
        ),
    )


def review(c, aid, agent, business):
    from .domain import metrics
    from .operating_contract import configuration
    from .operating_plans import EVENT_NAMES, acquisition
    from .operating_playbooks import planning_context

    measured = metrics(c)
    acquired = acquisition(c, agent, business, datetime.now(timezone.utc))
    targets = all_rows(
        c,
        "SELECT t.id AS target_id,s.platform,t.verified_permissions FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.state='active' AND s.state='connected'",
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
        "acquisition": acquired,
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
    if acquired.get("clicks_only"):
        decision, reason = "traffic_mode", "按实际发布后同帖龄的产品短链点击安排探索、回访和后续分发。"
        observations = {}
    elif not acquired["conversion_optimization"]:
        decision, reason = (
            "traffic_mode",
            "回传或目标事件尚未完整，平台展示接入状态；Agent 继续内容、分发和引流，不重复研究接入。",
        )
    else:
        decision, reason = (
            "observing",
            "保留当前目标口径，等待可比较的成熟观察；其他独立工作继续。",
        )
    platforms = {str(t["target_id"]): t["platform"] for t in targets}
    for target, observed in observations.items():
        stats = observed.get("stats", [])
        objective = observed.get("objective", measured.get("conversion_objective"))
        aligned = acquired["objective_ready"] and acquired[
            "objective_event"
        ] == EVENT_NAMES.get(objective, objective)
        if (
            observed["state"] == "provisional_preference"
            and aligned
            and observed.get("coverage") == "connected"
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
                    platform=platforms.get(target),
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
                        platform=platforms.get(target),
                    )
                )
            elif aligned and not sum(s.get("outcomes", 0) for s in stats):
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
                        platform=platforms.get(target),
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
        "acquisition": acquired,
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
    if task["input"].get("experiment_id"):
        return result  # Trial state owns the next action.
    kind = task["input"]["kind"]
    follow = {
        "measurement": "journey",
        "journey": "opportunities",
        "opportunities": "strategy",
        "validate": "strategy",
    }.get(kind)
    if (
        follow
        and result.get("status") != "unchanged"
        and (kind == "measurement" or result.get("new_evidence_keys"))
    ):
        key = ""
        if follow == "strategy":
            key = "evidence:" + fingerprint(sorted(result["new_evidence_keys"]))
            if task["input"].get("work_key", "").startswith("alternatives:"):
                key = "alternatives:" + key
        tid = enqueue(
            c,
            aid,
            agent,
            business,
            follow,
            "根据已完成的" + kind + "继续验证独立路径。",
            source_review_id=task["input"].get("latest_review_id")
            or task["input"].get("source_review_id"),
            platform=task["input"].get("platform"),
            work_key=key,
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
    """Recover a lost handoff only for committed, genuinely new evidence."""
    completed = all_rows(
        c,
        """SELECT id,input,result FROM lead.tasks
      WHERE type IN ('operating.journey','operating.opportunities','operating.validate')
      AND state='succeeded' AND result->>'status'='observed'
      AND input->>'progress_version'='1' AND input->>'business_profile_id'=%s
      AND input->>'config_revision'=%s AND input->>'control_version'=%s
      AND NOT (input ? 'experiment_id') AND NOT (result ? 'next_task_id') AND result->'new_evidence_keys'<>'[]'::jsonb""",
        (
            str(business["id"]),
            str(agent["config_revision"]),
            str(agent["control_version"]),
        ),
    )
    for task in completed:
        result = after_task(c, aid, task, task["result"])
        c.execute(
            "UPDATE lead.tasks SET result=%s WHERE id=%s", (j(result), task["id"])
        )
