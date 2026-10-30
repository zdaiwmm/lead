"""Product-owned goal/priority loop. Work is bounded, scoped and durable."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import all_rows, j, one, record, require_agent, tx, uid
from .operating_contract import configuration
from .operating_feedback import (
    after_task,
    enqueue,
    fingerprint,
    reconcile_handoffs,
    review,
)
from .scope import current_tenant

WORK_KINDS = {"measurement", "journey", "opportunities", "strategy", "validate"}
TICK_SECONDS = 30


@dataclass
class Candidate:
    key: str
    kind: str
    priority: int
    reason: str
    task_id: str | None = None
    estimated_token_units: int = 0
    value: int = 0
    risk_penalty: int = 0
    budget_penalty: int = 0


def blocked(c):
    """Unknown writes fence their target/account; unknown scope fences writes only."""
    pending = all_rows(
        c,
        "SELECT id,type,input FROM lead.tasks WHERE state IN ('queued','running','reconciling') AND (type='account.prepare' OR type='connection.verify_publish' OR type LIKE '%%.publish' OR type='community.engage')",
    )
    targets, accounts, unknown = set(), set(), False
    for row in pending:
        payload = row["input"]
        if payload.get("target_id"):
            targets.add(str(payload["target_id"]))
        if payload.get("account_id"):
            accounts.add(str(payload["account_id"]))
        if not payload.get("target_id") and not payload.get("account_id"):
            unknown = True
    for row in all_rows(
        c,
        "SELECT a.target_id,t.account_id FROM lead.actions a JOIN lead.account_targets t ON t.id=a.target_id WHERE a.state IN ('dispatching','unknown')",
    ):
        targets.add(str(row["target_id"]))
        accounts.add(str(row["account_id"]))
    return {
        "targets": sorted(targets),
        "accounts": sorted(accounts),
        "unknown_write_scope": unknown,
        "task_ids": [str(r["id"]) for r in pending],
    }


def target_blocked(target, barriers):
    return (
        barriers["unknown_write_scope"]
        or str(target["target_id"]) in barriers["targets"]
        or str(target["account_id"]) in barriers["accounts"]
    )


def candidates(c, aid, agent, business, now=None):
    from .admin_settings import effective
    from .community_schedule import deadlines
    from .content_backlog import next_due as content_due
    from .operating_admission import assess, context
    from .operating_work import REPORT_REVISION

    now = now or datetime.now(timezone.utc)
    tools, _ = effective(c, "models", aid)
    barriers = blocked(c)
    targets = all_rows(
        c,
        """SELECT t.id AS target_id,t.account_id,s.platform FROM lead.account_targets t
      JOIN lead.social_accounts s ON s.id=t.account_id JOIN lead.authorization_grants g ON g.target_id=t.id
      JOIN lead.platform_capabilities p ON p.platform=s.platform AND p.target_type=t.target_type AND p.action_type='publish'
      WHERE s.state='connected' AND s.automation_enabled AND t.state='active' AND g.revoked_at IS NULL
      AND g.allowed_actions ? 'publish' AND p.technical_state='verified' AND p.permission_state='approved'
      AND (s.platform='lab' OR (t.verified_permissions->>'adapter_version'=p.adapter_version AND jsonb_array_length(coalesce(t.verified_permissions->'publishing_verified_formats','[]'))>0))""",
    )
    eligible = [t for t in targets if not target_blocked(t, barriers)]
    reviewed = review(c, aid, agent, business)
    reconcile_handoffs(c, aid, agent, business, reviewed["review_id"])
    if not eligible:
        if business.get("product_url") and tools["tools"]["public_web"]:
            enqueue(
                c,
                aid,
                agent,
                business,
                "journey",
                "当前分发路径受限，先核对产品承接与独立入口。",
            )
        if tools["tools"]["public_web"]:
            enqueue(
                c,
                aid,
                agent,
                business,
                "opportunities",
                "当前缺少可执行分发路径，寻找与原渠道独立的需求和触达线索。",
            )
    c.execute(
        """UPDATE lead.tasks SET state='canceled',completed_at=now(),result=%s WHERE type LIKE 'operating.%%'
      AND state IN ('planned','failed') AND (input->>'business_profile_id'<>%s OR input->>'config_revision'<>%s OR input->>'control_version'<>%s)""",
        (
            j({"reason": "source_version_changed"}),
            str(business["id"]),
            str(agent["config_revision"]),
            str(agent["control_version"]),
        ),
    )
    busy = bool(
        one(
            c,
            "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('requested','active','ending') AND expires_at>now()",
        )
        or one(
            c,
            "SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()",
        )
        or one(
            c,
            "SELECT 1 FROM lead.browser_runs WHERE state='running' AND message_id IS NOT NULL",
        )
    )
    previous_kernel = agent["onboarding_state"].get("operating_kernel", {})
    same_sources = (
        previous_kernel.get("source_revision") == agent["config_revision"]
        and previous_kernel.get("source_control") == agent["control_version"]
        and previous_kernel.get("business_profile_id") == str(business["id"])
    )
    lanes = previous_kernel.get("lanes", {}) if same_sources else {}
    result = []
    admission = context(c, aid, business)
    deferred = []

    def add(
        key,
        kind,
        priority,
        reason,
        browser=False,
        task_id=None,
        contract_repair=False,
        task=None,
    ):
        previous = lanes.get(key, {})
        if browser and busy:
            return
        if (
            previous.get("next_at")
            and datetime.fromisoformat(previous["next_at"]) > now
            and not contract_repair
            and previous.get("reason") != "token_budget_wait"
        ):
            return
        preferred = configuration(agent, business)["preferred_work"]
        boost = 15 if kind.removeprefix("operating.") in preferred else 0
        rating = assess(admission, kind, priority + boost, task)
        if not rating["allowed"]:
            deferred.append({"key": key, "kind": kind, **rating})
            return
        result.append(
            Candidate(
                key,
                kind,
                rating["priority"],
                reason,
                task_id,
                rating["estimated_token_units"],
                rating["value"],
                rating["risk_penalty"],
                rating["budget_penalty"],
            )
        )

    if eligible:
        add(
            "publish:" + fingerprint(sorted(str(t["target_id"]) for t in eligible)),
            "publish",
            90,
            "存在范围内已验证且不受原未知动作影响的目标。",
            browser=True,
        )
    work = all_rows(
        c,
        "SELECT * FROM lead.tasks WHERE type LIKE 'operating.%%' AND state IN ('planned','failed','running') ORDER BY created_at",
    )
    # New follow-ups use the transaction timestamp. Include those inserted by
    # this perception pass rather than deferring them behind low-value work.
    now = max(now, datetime.now(timezone.utc))
    active = [
        w
        for w in work
        if w["input"].get("kind") in WORK_KINDS
        and w["state"] == "running"
        and w["started_at"] > now - timedelta(minutes=5)
    ]
    if not active:
        for work in work:
            kind = work["input"].get("kind")
            if kind not in WORK_KINDS:
                continue
            attempts = (work["result"] or {}).get("attempts", 0)
            contract_repair = (
                work["state"] == "failed"
                and (
                    (work["result"] or {}).get("error_type")
                    == "UnexpectedModelBehavior"
                    or (
                        kind in ("journey", "opportunities", "validate")
                        and (work["result"] or {}).get("error_code")
                        == "OPERATING_SOURCE_MISMATCH"
                    )
                )
                and (work["result"] or {}).get("report_revision", 1) < REPORT_REVISION
            )
            if attempts >= 2 or (work["scheduled_at"] > now and not contract_repair):
                continue
            if (
                kind in ("journey", "opportunities", "validate")
                and not tools["tools"]["public_web"]
            ):
                continue
            age = min(12, int((now - work["created_at"]).total_seconds() / 1800))
            add(
                str(work["id"]),
                work["type"],
                {
                    "measurement": 85,
                    "journey": 72,
                    "opportunities": 76,
                    "strategy": 68,
                    "validate": 78,
                }[kind]
                + age,
                "报告契约已修正，在原次数与额度内恢复同一只读任务。"
                if contract_repair
                else work["input"]["reason"],
                browser=kind in ("journey", "opportunities", "validate"),
                task_id=str(work["id"]),
                contract_repair=contract_repair,
                task=work,
            )
    due = content_due(c, agent, now)
    if due and due <= now:
        add("content", "content", 65, "储备尚未充足，准备下一篇具体内容。")
    due = deadlines(c, agent, now)
    if due and due.get("next_discovery_at") and due["next_discovery_at"] <= now:
        add(
            "community_discovery",
            "community_discovery",
            70,
            "已启用的公开需求研究已到期。",
            browser=True,
        )
    if (
        agent["onboarding_state"].get("community_growth", {}).get("mode")
        == "contextual_outreach"
    ):
        add(
            "community_progress",
            "community_progress",
            75,
            "核对已授权的社群任务与回访排期。",
            browser=True,
        )
    if one(
        c,
        "SELECT 1 FROM lead.tasks WHERE type='account.prepare' AND state IN ('queued','running','waiting_user')",
    ) or one(
        c,
        "SELECT 1 FROM lead.social_accounts s WHERE state='connected' AND automation_enabled AND NOT EXISTS(SELECT 1 FROM lead.account_targets t WHERE t.account_id=s.id AND t.state='active')",
    ):
        add("account", "account", 74, "继续用户原已启动的账号筹备。", browser=True)
    if not one(
        c,
        "SELECT 1 FROM lead.tasks WHERE type='channel.prepare' AND result->>'business_profile_id'=%s AND result->>'config_revision'=%s",
        (str(business["id"]), str(agent["config_revision"])),
    ):
        add("kits", "kits", 60, "为当前资料版本准备通用渠道底稿。")
    reviewed = {
        **reviewed,
        "scheduling": {
            "budget": admission["budget"],
            "deferred": deferred,
            "model_settings_version": admission["model_settings_version"],
        },
    }
    return sorted(result, key=lambda v: (-v.priority, v.key)), barriers, reviewed


async def run_work(aid, candidate):
    from .budget import BudgetExceeded
    from .operating_work import REPORT_REVISION, execute

    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        task = one(
            c, "SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE", (candidate.task_id,)
        )
        if agent["desired_state"] != "running" or task["state"] not in (
            "planned",
            "failed",
            "running",
        ):
            return {"delay": 30, "reason": "work_changed"}
        latest = one(
            c, "SELECT id FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if (
            agent["config_revision"] != task["input"]["config_revision"]
            or agent["control_version"] != task["input"]["control_version"]
            or str(latest["id"]) != task["input"]["business_profile_id"]
        ):
            c.execute(
                "UPDATE lead.tasks SET state='canceled',completed_at=now(),result=%s WHERE id=%s",
                (j({"reason": "source_version_changed"}), task["id"]),
            )
            return {"delay": 30, "reason": "source_version_changed"}
        attempts = (task["result"] or {}).get("attempts", 0) + 1
        c.execute(
            "UPDATE lead.tasks SET state='running',started_at=now(),result=%s WHERE id=%s",
            (j({**(task["result"] or {}), "attempts": attempts}), task["id"]),
        )
    try:
        result = await asyncio.wait_for(execute(aid, task), timeout=190)
        with tx(aid) as c:
            current = one(
                c, "SELECT * FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,)
            )
            business = one(
                c,
                "SELECT id FROM lead.business_profiles ORDER BY revision DESC LIMIT 1",
            )
            if (
                current["desired_state"] != "running"
                or current["config_revision"] != task["input"]["config_revision"]
                or current["control_version"] != task["input"]["control_version"]
                or str(business["id"]) != task["input"]["business_profile_id"]
            ):
                c.execute(
                    "UPDATE lead.tasks SET state='canceled',completed_at=now(),result=%s WHERE id=%s",
                    (j({"reason": "configuration_changed"}), task["id"]),
                )
                return {"delay": 30, "reason": "configuration_changed"}
            result.update(
                {
                    "attempts": attempts,
                    "report_revision": REPORT_REVISION,
                    "source_review_id": task["input"].get("source_review_id"),
                }
            )
            # Complete before making a successor; otherwise the deduplicator
            # could bind a follow-up to the task which is currently running.
            c.execute(
                "UPDATE lead.tasks SET state='succeeded',completed_at=now(),result=%s WHERE id=%s",
                (j(result), task["id"]),
            )
            result = after_task(c, aid, task, result)
            c.execute(
                "UPDATE lead.tasks SET result=%s WHERE id=%s", (j(result), task["id"])
            )
            record(
                c,
                aid,
                "自主运营工作已完成",
                result.get("title", candidate.kind),
                task_id=task["id"],
            )
        return {
            "delay": TICK_SECONDS,
            "operating_task_id": str(task["id"]),
            "reason": "operating_work_completed",
        }
    except BudgetExceeded as error:
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET state='planned',scheduled_at=now(),result=result || %s WHERE id=%s",
                (
                    j(
                        {
                            "attempts": attempts - 1,
                            "required_token_units": error.required_units
                            or candidate.estimated_token_units,
                            "deferred_reason": "token_budget_insufficient",
                        }
                    ),
                    task["id"],
                ),
            )
        return {"delay": 30, "reason": "token_budget_wait"}
    except Exception as error:
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET state='failed',completed_at=now(),scheduled_at=now()+interval '15 minutes',result=result || %s WHERE id=%s",
                (
                    j(
                        {
                            "attempts": attempts,
                            "report_revision": REPORT_REVISION,
                            "error_type": type(error).__name__,
                            "error_code": str(error)
                            if str(error)
                            in {
                                "NO_VERIFIABLE_PUBLIC_OBSERVATION",
                                "OPERATING_SOURCE_MISMATCH",
                                "STRATEGY_SOURCE_MISMATCH",
                                "PUBLIC_PRODUCT_URL_REQUIRED",
                                "PUBLIC_ACCESS_CHALLENGE",
                                "PUBLIC_CONTENT_UNAVAILABLE",
                            }
                            else "OPERATING_WORK_UNAVAILABLE",
                            "retry_safe": True,
                            **(
                                {
                                    "blocked_host": error.blocked_host,
                                    "risk_retry_at": (
                                        datetime.now(timezone.utc)
                                        + timedelta(minutes=30)
                                    ).isoformat(),
                                }
                                if getattr(error, "blocked_host", None)
                                else {}
                            ),
                        }
                    ),
                    task["id"],
                ),
            )
            record(
                c,
                aid,
                "运营任务等待恢复",
                type(error).__name__ + "；其他独立任务继续。",
                "warning",
                task_id=task["id"],
            )
        return {"delay": 30, "reason": "operating_work_cooldown"}


async def cycle(args):
    aid, cycle_id = args["agent_id"], args["cycle_id"]
    now = datetime.now(timezone.utc)
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        if agent["desired_state"] != "running":
            return {
                "delay": 86400,
                "idle": True,
                "archived": agent["desired_state"] == "archived",
            }
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if not business or not business["description"].strip():
            return {"delay": 3600, "reason": "business_required"}
        old = one(
            c,
            "SELECT result FROM lead.tasks WHERE workflow_id=%s",
            ("operating-decision:" + cycle_id,),
        )
        if old:
            return {
                **old["result"].get(
                    "cycle_result", {"delay": 30, "reason": "decision_recovery"}
                ),
                "duplicate": True,
            }
        choices, barriers, feedback = candidates(c, aid, agent, business, now)
        selected = choices[0] if choices else None
        decision = {
            "engine": "value_budget_risk_v2",
            "goal": configuration(agent, business),
            "budget": feedback["scheduling"]["budget"],
            "deferred_candidates": feedback["scheduling"]["deferred"],
            "source_revision": agent["config_revision"],
            "source_control": agent["control_version"],
            "business_profile_id": str(business["id"]),
            "blocked": barriers,
            "review_id": feedback["review_id"],
            "selected": selected.__dict__ if selected else None,
            "candidates": [x.__dict__ for x in choices],
            "next_at": (
                now + timedelta(seconds=TICK_SECONDS if selected else 300)
            ).isoformat(),
            "lanes": agent["onboarding_state"]
            .get("operating_kernel", {})
            .get("lanes", {})
            if agent["onboarding_state"]
            .get("operating_kernel", {})
            .get("source_revision")
            == agent["config_revision"]
            and agent["onboarding_state"]
            .get("operating_kernel", {})
            .get("source_control")
            == agent["control_version"]
            else {},
        }
        tid = uid()
        c.execute(
            "INSERT INTO lead.tasks(id,tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,started_at) VALUES(%s,%s,%s,'operating.decision','planned',%s,%s,%s,now(),now())",
            (
                tid,
                current_tenant(),
                aid,
                "operating-decision:" + cycle_id,
                j(
                    {
                        "business_profile_id": str(business["id"]),
                        "config_revision": agent["config_revision"],
                    }
                ),
                j(decision),
            ),
        )
    result = {"delay": 300, "idle": True, "reason": "all_work_waiting_or_saturated"}
    if not selected and any(
        x["reason"] == "token_budget_insufficient"
        for x in decision["deferred_candidates"]
    ):
        result.update(
            reason="token_budget_wait",
            next_budget_reset_at=decision["budget"]["period_ends_at"],
        )
    try:
        if selected:
            if selected.task_id:
                result = await run_work(aid, selected)
            elif selected.kind == "publish":
                from .activities import _publication_cycle

                result = await _publication_cycle(args)
            elif selected.kind == "content":
                from .content_backlog import prepare_one

                result = (await prepare_one(aid)) or {
                    "delay": 300,
                    "reason": "content_reserve_full",
                }
                result.setdefault("delay", 30)
            elif selected.kind == "kits":
                from .operating_workspace import prepare_all

                packs = await asyncio.to_thread(
                    prepare_all, aid, continue_on_error=True
                )
                result = {
                    "delay": 30,
                    "prepared_channels": sum("plans" in p for p in packs),
                }
            elif selected.kind == "community_discovery":
                from .community_growth import discover_communities

                result = (await discover_communities(aid)) or {"delay": 300}
            elif selected.kind == "community_progress":
                from .activities import browser_rpc
                from .community_runtime import community_cycle

                result = (await community_cycle(aid, browser_rpc)) or {"delay": 300}
            elif selected.kind == "account":
                from .account_setup import (
                    prepare_accounts,
                    prepare_requested_channel,
                    recover_saved_login,
                )
                from .activities import browser_rpc

                await recover_saved_login(aid, browser_rpc)
                result = (
                    (await prepare_requested_channel(aid, browser_rpc))
                    or (await prepare_accounts(aid, browser_rpc))
                    or {"delay": 300}
                )
    except Exception as error:
        result = {
            "delay": 300,
            "reason": "lane_temporarily_unavailable",
            "error_type": type(error).__name__,
        }
    with tx(aid) as c:
        current = one(
            c, "SELECT * FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,)
        )
        if current["desired_state"] == "archived":
            return {"delay": 86400, "archived": True, "idle": True}
        state = decision
        if selected:
            state["lanes"][selected.key] = {
                "checked_at": now.isoformat(),
                "next_at": (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=max(5, result.get("delay", 300)))
                ).isoformat(),
                "reason": result.get("reason"),
            }
        # Independent ready work runs on the next product timer, even when the
        # selected lane returned a long account/research backoff.
        delay = min(300, max(5, result.get("delay", 300)))
        if len(choices) > 1:
            delay = min(delay, TICK_SECONDS)
        state["next_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
        state["last_result"] = result
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
            (j({"operating_kernel": state}), aid),
        )
        c.execute(
            "UPDATE lead.tasks SET state='succeeded',completed_at=now(),result=%s WHERE id=%s",
            (j({**state, "cycle_result": {**result, "delay": delay}}), tid),
        )
    return {
        **result,
        "delay": delay,
        "decision_id": tid,
        "selected_work": selected.kind if selected else None,
    }


def status(c, agent):
    value = agent["onboarding_state"].get("operating_kernel", {})
    queued = all_rows(
        c,
        "SELECT id,type,state,input,result,scheduled_at FROM lead.tasks WHERE type LIKE 'operating.%%' AND type<>'operating.decision' AND state IN ('planned','running','failed') ORDER BY created_at LIMIT 12",
    )
    return {
        "engine": "value_budget_risk_v2",
        "decision": value,
        "queued_operating_work": queued,
        "recent_operating_work": all_rows(
            c,
            "SELECT id,type,state,result,completed_at FROM lead.tasks WHERE type LIKE 'operating.%%' AND type<>'operating.decision' AND state IN ('succeeded','canceled') ORDER BY completed_at DESC LIMIT 8",
        ),
        "latest_review": one(
            c,
            "SELECT id,body,created_at FROM lead.memory_entries WHERE kind='observation' AND body->>'type'='operating_review' ORDER BY created_at DESC LIMIT 1",
        ),
    }
