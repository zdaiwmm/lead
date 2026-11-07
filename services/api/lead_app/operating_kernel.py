"""Product-owned goal/priority loop. Work is bounded, scoped and durable."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import growth_experiments as trials
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

WORK_KINDS = {"journey", "opportunities", "strategy", "validate"}
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
    target_id: str | None = None
    experiment_id: str | None = None
    delivery_ready: bool = False
    community_operation: dict | None = None


def blocked(c, *, community_worker=False):
    """Unknown writes fence their target/account; unknown scope fences writes only."""
    from .publication_recovery import pending_writes, stale_identity_tasks

    pending = pending_writes(c)
    if community_worker:
        # The worker resumes these intents itself, including uncertain sends.
        # Other workers still see them as account fences.
        pending = [row for row in pending if row["type"] != "community.engage"]
    stale = stale_identity_tasks(c)
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
        "SELECT a.task_id,a.target_id,t.account_id FROM lead.actions a JOIN lead.account_targets t ON t.id=a.target_id WHERE a.state IN ('dispatching','unknown')",
    ):
        targets.add(str(row["target_id"]))
        if str(row["task_id"]) not in stale:
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


def publication_lane_key(targets):
    return "publish:" + fingerprint(sorted(str(t["target_id"]) for t in targets))


def eligible_publish_targets(c, barriers):
    targets = all_rows(
        c,
        """SELECT t.id AS target_id,t.account_id,s.platform,t.verified_permissions->'owned_audience' AS owned_audience,t.verified_permissions->>'channel_language' AS channel_language,
      t.verified_permissions->'publishing_verified_formats' AS formats FROM lead.account_targets t
      JOIN lead.social_accounts s ON s.id=t.account_id JOIN lead.authorization_grants g ON g.target_id=t.id
      JOIN lead.platform_capabilities p ON p.platform=s.platform AND p.target_type=t.target_type AND p.action_type='publish'
      WHERE s.state='connected' AND s.automation_enabled AND t.state='active' AND g.revoked_at IS NULL
      AND g.allowed_actions ? 'publish' AND p.technical_state='verified' AND (p.permission_state='approved' OR p.permission_state='pending' AND lead.owner_browser_pilot_allowed(t.id,g.id))
      AND (s.platform='lab' OR (t.verified_permissions->>'adapter_version'=p.adapter_version AND jsonb_array_length(coalesce(t.verified_permissions->'publishing_verified_formats','[]'))>0))""",
    )
    return [t for t in targets if not target_blocked(t, barriers)]


def candidates(c, aid, agent, business, now=None):
    from .admin_settings import effective
    from .community_schedule import deadlines
    from .content_backlog import next_due as content_due
    from .operating_admission import assess, context
    from .operating_progress import seed_history
    from .operating_work import REPORT_REVISION

    now = now or datetime.now(timezone.utc)
    tools, _ = effective(c, "models", aid)
    seed_history(c, aid, agent, business)
    barriers = blocked(c)
    eligible = eligible_publish_targets(c, barriers)
    reviewed = review(c, aid, agent, business)
    pilot = trials.enabled(agent)
    experiments = trials.advance(c, aid, agent, business, now) if pilot else []
    from .operating_feedback import coalesce_environment_work

    coalesce_environment_work(
        c,
        {
            "business_profile_id": str(business["id"]),
            "config_revision": agent["config_revision"],
        },
    )
    c.execute(
        """UPDATE lead.tasks SET state='canceled',completed_at=now(),result=coalesce(result,'{}'::jsonb) || %s
        WHERE type='operating.measurement' AND state IN ('planned','failed')""",
        (j({"reason": "platform_owned_dependency", "not_a_business_outcome": True}),),
    )
    c.execute(
        """UPDATE lead.tasks SET state='canceled',completed_at=now(),result=coalesce(result,'{}'::jsonb) || %s WHERE type LIKE 'operating.%%'
      AND state IN ('planned','failed') AND (coalesce(input->>'progress_version','')<>'1' OR input->>'business_profile_id'<>%s OR input->>'config_revision'<>%s OR input->>'control_version'<>%s)""",
        (
            j({"reason": "source_version_changed"}),
            str(business["id"]),
            str(agent["config_revision"]),
            str(agent["control_version"]),
        ),
    )
    public_web = tools["tools"]["public_web"]
    from .adapters.catalog import SPECS
    from .operating_followups import reconcile

    channels = [
        row["platform"]
        for row in all_rows(
            c,
            "SELECT DISTINCT platform FROM lead.social_accounts WHERE state='connected' AND automation_enabled ORDER BY platform",
        )
        if row["platform"] in SPECS
    ]
    reconcile_handoffs(c, aid, agent, business, reviewed["review_id"])
    if channels:
        # Old open work remains auditable. Do not spend another tick on an
        # unrelated URL because it was queued before a channel was connected.
        c.execute(
            """UPDATE lead.tasks SET state='canceled',completed_at=now(),result=result || %s
            WHERE type IN ('operating.opportunities','operating.validate','operating.strategy')
            AND input->>'progress_version'='1' AND input->>'target_id' IS NULL
            AND coalesce(input->>'work_key','') NOT LIKE 'alternatives:%%'
            AND NOT (coalesce(input->>'platform','')=ANY(%s))
            AND (state IN ('planned','failed') OR (state='running' AND started_at<now()-interval '5 minutes'))""",
            (j({"reason": "channel_scope_changed"}), channels),
        )
    if public_web:
        if not eligible and business.get("product_url") and not trials.chamber_campaign(agent):
            enqueue(c, aid, agent, business, "journey", "核对当前业务承接入口。")
        # Telegram has its own checkpointed group/rules discovery and distinct
        # participation grants. Other connected adapters use scoped public research.
        for platform in channels or [None]:
            if platform == "telegram":
                continue
            if not reconcile(c, aid, agent, business, reviewed["review_id"], platform):
                enqueue(
                    c,
                    aid,
                    agent,
                    business,
                    "opportunities",
                    f"寻找与业务匹配的 {platform or '公开'} 讨论、分享或分发入口，核对参与规则。",
                    work_key="public-discovery",
                    platform=platform,
                )
        if channels and not reconcile(c, aid, agent, business, reviewed["review_id"], alternatives=True):
            enqueue(
                c, aid, agent, business, "opportunities",
                "寻找当前自有渠道之外适合本业务的公开收录、投稿或合作入口，核对受众和参与条件。",
                work_key="alternatives:public-discovery",
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
    admission["first_users"] = trials.first_users(agent)
    deferred = []
    historical_cost = {}
    pilot_wallet = {}
    if pilot:
        pilot_wallet = trials.allowances(c,agent,admission["budget"]["limit_units"],now)
        estimates = all_rows(c, """WITH recent AS (
            SELECT e.units,r.pricing_snapshot->'trial'->>'kind' AS kind,
            row_number() OVER(PARTITION BY r.pricing_snapshot->'trial'->>'kind' ORDER BY e.occurred_at DESC) AS n
            FROM lead.usage_events e JOIN lead.budget_reservations r ON r.id=e.reservation_id
            WHERE e.category='model_tokens' AND r.pricing_snapshot ? 'trial')
            SELECT kind, percentile_disc(0.75) WITHIN GROUP(ORDER BY units) AS p75
            FROM recent WHERE n<=20 GROUP BY kind HAVING count(*)>=5""")
        historical_cost = {r["kind"]: r["p75"] for r in estimates}

    def add(
        key,
        kind,
        priority,
        reason,
        browser=False,
        task_id=None,
        contract_repair=False,
        task=None,
        target_id=None,
        delivery_ready=False,
    ):
        from .resource_pressure import sample

        if kind in {"community_discovery", "content", "kits", "operating.opportunities"} and sample()["defer_optional"]:
            deferred.append({"key": key, "kind": kind, "reason": "host_pressure", "allowed": False})
            return
        previous = lanes.get(key, {})
        if browser and busy:
            return
        if (
            previous.get("next_at")
            and datetime.fromisoformat(previous["next_at"]) > now
            and not contract_repair
            and not delivery_ready
            and previous.get("reason") != "token_budget_wait"
        ):
            return
        preferred = configuration(agent, business)["preferred_work"]
        boost = 15 if kind.removeprefix("operating.") in preferred else 0
        experiment_id = (task or {}).get("input", {}).get("experiment_id")
        if experiment_id and (not pilot or not any(r["body"]["id"] == experiment_id
            and r["body"]["state"] == "executing" and r["body"]["task_ids"][-1:] == [task_id]
            for r in experiments)):
            return
        paid_step = None
        community_operation = None
        if pilot and kind == "publish":
            from .content_backlog import ready
            owned = next((r for r in experiments if r["body"]["state"]=="awaiting_publication" and not r["body"].get("publication_task_id")),None)
            target = next((t for t in eligible if str(t["target_id"])==target_id),{})
            drafts = ready(c, business["id"],agent["config_revision"])
            reusable = any(v["input"]["platform"]==target.get("platform") and
                           v["input"]["format"] in (target.get("formats") or []) and
                           (not target.get("channel_language") or v["input"].get("channel_language")==target["channel_language"])
                           for v in drafts)
            if owned and (admission["budget"].get("acquisition_uncapped") or
                          min(admission["budget"]["available_units"],trials.headroom(pilot_wallet,"execution",True)) >= 30000) and not one(c,
                """SELECT 1 FROM lead.tasks WHERE type='account.prepare' AND result->>'awaiting_first_publish'='true'
                    AND result->>'target_id'=%s LIMIT 1""",(target_id,)):
                experiment_id = owned["body"]["id"]
            paid_step = bool(experiment_id) or not reusable
        elif kind == "community_progress":
            from .community_growth import GrowthConfig
            from .community_selection import choose
            community_operation = choose(c, agent, GrowthConfig.model_validate(agent["onboarding_state"].get("community_growth", {})), now)
            if not community_operation:
                return
            paid_step = community_operation["paid"]
        request_units = None
        if pilot and kind == "operating.strategy":
            from .model import managed_prompt, reservation_units
            from .operating_work import acquisition_review_prompt, is_acquisition_review

            if task and is_acquisition_review(task):
                prompt = acquisition_review_prompt(c, agent, business, task)
                if prompt["recent_work"]:
                    settings = admission["models"]
                    encoded = json.dumps(
                        managed_prompt(aid, prompt, settings),
                        ensure_ascii=False,
                        default=str,
                    )
                    request_units = reservation_units(
                        encoded, min(1800, settings["max_output_tokens"])
                    )
        if request_units is not None:
            rating = assess(
                admission, kind, priority + boost, task, request_units=request_units
            )
        elif pilot:
            rating = assess(
                admission, kind, priority + boost, task, paid_step=paid_step
            )
        else:
            rating = assess(admission, kind, priority + boost, task)
        if not rating["allowed"]:
            deferred.append({"key": key, "kind": kind, **rating})
            return
        if (
            pilot
            and request_units is None
            and rating["estimated_token_units"]
            and kind in historical_cost
        ):
            rating["estimated_token_units"] = max(
                int(historical_cost[kind]),
                (task or {}).get("result", {}).get("required_token_units", 0)
                if (task or {}).get("result")
                else 0,
            )
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
                target_id,
                experiment_id,
                delivery_ready,
                community_operation,
            )
        )

    # On rollout or connection of a second channel, persistent plans predate
    # the per-target lane. Reconstruct the timer before choosing any work.
    publish_schedule = {}
    if eligible:
        scheduled = all_rows(
            c,
            """SELECT DISTINCT ON (p.body->>'target_id') p.body->>'target_id' AS target_id,
              p.created_at + make_interval(secs=>coalesce((p.body->>'next_delay_seconds')::int,14400)) AS due
            FROM lead.plans p JOIN lead.agent_revisions r ON r.id=p.agent_revision_id
            WHERE p.business_profile_id=%s AND r.revision=%s AND p.body->>'target_id'=ANY(%s::text[])
              AND NOT EXISTS(SELECT 1 FROM lead.tasks t WHERE t.type='account.prepare' AND t.state='succeeded'
                AND t.input->>'fresh_channel'='true' AND t.result->>'awaiting_first_publish'='true'
                AND t.result->>'target_id'=p.body->>'target_id')
            ORDER BY p.body->>'target_id',p.created_at DESC,p.revision DESC""",
            (business["id"], agent["config_revision"],
             [str(t["target_id"]) for t in eligible if t["platform"] in ("telegram", "lab")]),
        )
        for row in scheduled:
            key = publication_lane_key([row])
            if row["due"] > now and not lanes.get(key, {}).get("next_at"):
                publish_schedule[key] = {"checked_at": now.isoformat(), "next_at": row["due"].isoformat(), "reason": "scheduled"}
    from .owned_audience import status as audience_status

    for target in eligible:
        if trials.first_users(agent) and target["platform"] == "telegram":
            audience = audience_status(target.get("owned_audience"), now)
            if audience == "due":
                target_id = str(target["target_id"])
                add("audience:" + target_id, "audience", 72, "确认频道是否已有可触达的受众。", browser=True, target_id=target_id)
            if audience != "eligible":
                continue
        if publication_lane_key([target]) in publish_schedule:
            continue
        add(
            publication_lane_key([target]),
            "publish",
            90,
            "存在范围内已验证且不受原未知动作影响的目标。",
            browser=True,
            target_id=str(target["target_id"]),
        )
    from .publication_recovery import due

    for recovery in due(c, now):
        add(
            "reconcile:" + str(recovery["id"]),
            "publication_reconcile",
            110,
            "核对原发布动作的真实回执，不重复提交。",
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
            if kind not in WORK_KINDS or (kind == "journey" and trials.chamber_campaign(agent)):
                continue
            if channels and kind in ("opportunities", "validate", "strategy"):
                platform = work["input"].get("platform")
                alternatives = not platform and work["input"].get("work_key", "").startswith("alternatives:")
                if platform not in channels and not work["input"].get("target_id") and not alternatives:
                    continue
                if platform == "telegram" and kind in ("opportunities", "validate"):
                    continue  # Telegram discovery uses the public-community adapter.
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
        add("content", "content", 90, "待执行的内容储备尚未充足，优先交付具体内容。")
    due = deadlines(c, agent, now)
    discovery_due = due and due.get("next_discovery_at") and due["next_discovery_at"] <= now
    if discovery_due:
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
        from .community_runtime import next_voice_edit
        correction_due = bool(next_voice_edit(c,agent))
        delivery_ready = bool(trials.chamber_campaign(agent) and one(c, """SELECT 1 FROM lead.tasks
            WHERE type='community.engage' AND state='running' AND input ? 'reply'
            AND (result->>'receipt_review_after' IS NULL OR (result->>'receipt_review_after')::timestamptz<=now())
            AND result->>'reply_state' IS NULL LIMIT 1"""))
        add(
            "community_progress",
            "community_progress",
            95 if correction_due else 75,
            "核对已授权的社群任务与回访排期。",
            browser=True,
            contract_repair=correction_due,
            delivery_ready=delivery_ready,
        )
    if one(
        c,
        "SELECT 1 FROM lead.tasks WHERE type='account.prepare' AND state IN ('queued','running','waiting_user')",
    ) or one(
        c,
        "SELECT 1 FROM lead.social_accounts s WHERE state='connected' AND automation_enabled AND NOT EXISTS(SELECT 1 FROM lead.account_targets t WHERE t.account_id=s.id AND t.state='active')",
    ):
        from .account_setup import SETUP_REVISION

        repaired = bool(
            one(
                c,
                "SELECT 1 FROM lead.tasks WHERE type='account.prepare' AND state='waiting_user' AND coalesce((result->>'setup_revision')::int,1)<%s",
                (SETUP_REVISION,),
            )
        )
        add(
            "account",
            "account",
            100,
            "继续用户原已启动的账号筹备。",
            browser=True,
            contract_repair=repaired,
        )

    if not one(
        c,
        "SELECT 1 FROM lead.tasks WHERE type='channel.prepare' AND result->>'business_profile_id'=%s AND result->>'config_revision'=%s",
        (str(business["id"]), str(agent["config_revision"])),
    ):
        add("kits", "kits", 60, "为当前资料版本准备通用渠道底稿。")
    reviewed = {
        **reviewed,
        "publish_schedule": publish_schedule,
        "scheduling": {
            "budget": admission["budget"],
            "deferred": deferred,
            "model_settings_version": admission["model_settings_version"],
        },
    }
    return sorted(result, key=lambda v: (-v.priority, v.key)), barriers, reviewed


async def run_work(aid, candidate, operating_plan=None):
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
        if operating_plan:
            task["input"]["operating_plan"] = operating_plan
            c.execute(
                "UPDATE lead.tasks SET input=%s WHERE id=%s",
                (j(task["input"]), task["id"]),
            )
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
            from .operating_progress import accept

            result = accept(c, aid, task, result)
            if result.get("status") in ("observed", "hypotheses"):
                c.execute(
                    "UPDATE lead.agents SET onboarding_state=onboarding_state-'model_backoff' WHERE id=%s",
                    (aid,),
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
        if str(error) == "experiment_limit" and candidate.experiment_id:
            with tx(aid) as c:
                c.execute("UPDATE lead.tasks SET state='canceled',completed_at=now(),result=result||%s WHERE id=%s",
                          (j({"error_code":"experiment_limit","limitations":"单项试验已达到预算，优先推进其他假设。"}),task["id"]))
            return {"delay":5,"reason":"experiment_limit"}
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
            if type(error).__name__ == "ModelRateLimitError" and not getattr(
                error, "lead_backoff_recorded", False
            ):
                from .admin_settings import effective

                _, model_version = effective(c, "models", aid)
                a = require_agent(c, aid, True)
                failures = (
                    a["onboarding_state"].get("model_backoff", {}).get("failures", 0)
                    + 1
                )
                c.execute(
                    "UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{model_backoff}',%s) WHERE id=%s",
                    (
                        j(
                            {
                                "failures": failures,
                                "model_settings_version": model_version,
                                "retry_at": (
                                    datetime.now(timezone.utc)
                                    + timedelta(
                                        seconds=min(3600, 60 * 2 ** min(failures, 6))
                                    )
                                ).isoformat(),
                            }
                        ),
                        aid,
                    ),
                )
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
                                "BROWSER_USE_UNAVAILABLE",
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
    from .async_db import run as run_db

    def prepare():
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
            if trials.chamber_campaign(agent):
                from .community_feedback import refresh_observations
                refresh_observations(c,aid,agent,business,now)
            choices, barriers, feedback = candidates(c, aid, agent, business, now)
            from .operating_plans import execution_reference, prioritize, sync

            plan = sync(c, aid, agent, business, now)
            plan_reference = execution_reference(plan)
            choices = prioritize(choices, plan["mode"], plan["week"]["focus"])
            config = trials.policy(agent)
            pilot = trials.enabled(agent)
            order_state = {}
            if config.get("mode") in ("active","shadow"):
                remaining = trials.allowances(c,agent,feedback["scheduling"]["budget"]["limit_units"],now)
                ordered, deferred, order_state = trials.decision_order(choices,agent["onboarding_state"].get("operating_kernel",{}).get("trial_order",{}),now,remaining)
                order_state["shadow_choice"] = ordered[0].key if ordered else None
                if pilot:
                    choices = ordered
                    feedback["scheduling"]["deferred"].extend(deferred)
            ready = next((choice for choice in choices if choice.delivery_ready), None)
            if ready and not any(choice.kind == "publication_reconcile" for choice in choices):
                choices = [ready, *(choice for choice in choices if choice is not ready)]
            selected = choices[0] if choices else None
            decision = {
                "weekly_plan_id": plan["week"]["id"],
                "daily_plan_id": plan["day"]["id"],
                "daily_plan_version": plan["day"]["version"],
                "acquisition_mode": plan["mode"]["mode"],
                "engine": "growth_trials_v1" if pilot else "value_budget_risk_v2",
                "trial_order": order_state,
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
            decision["lanes"].update(feedback.get("publish_schedule", {}))
            tid = uid()
            action_context = None
            if pilot and selected:
                trial_id = selected.experiment_id
                maximum = None
                if trial_id:
                    trial = one(c, "SELECT body FROM lead.memory_entries WHERE id=%s", (trial_id,))["body"]
                    maximum = trial.get("max_tokens")
                    plan_reference.update(experiment_id=trial_id, hypothesis=trial["mechanism"], audience=trial["audience"], action=trial["next_action"], trial_custom_content=selected.kind=="publish")
                else:
                    trial_id=trials.existing_work(c,aid,agent,business,selected,now)
                    plan_reference["experiment_id"]=trial_id
                action_context = {"aid":aid,"id":trial_id,"decision_id":tid,"kind":selected.kind,
                                  "bucket":trials.bucket(selected.kind),"action":selected.key,"max_tokens":maximum,
                                  "revision":agent["config_revision"],"control":agent["control_version"]}
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
        return {"context": (agent, business, choices, selected, decision, tid, action_context, plan_reference)}

    prepared = await run_db(prepare)
    if "context" not in prepared:
        return prepared
    agent, business, choices, selected, decision, tid, action_context, plan_reference = prepared["context"]
    result = {"delay": 300, "idle": True, "reason": "all_work_waiting_or_saturated"}
    if not selected and any(
        x["reason"] == "token_budget_insufficient"
        for x in decision["deferred_candidates"]
    ):
        result.update(
            reason="token_budget_wait",
            next_budget_reset_at=decision["budget"]["period_ends_at"],
        )
    if not selected and any(x["reason"]=="experiment_working_budget" for x in decision["deferred_candidates"]):
        result.update(reason="experiment_working_budget",next_budget_reset_at=decision["budget"]["period_ends_at"])
    if trials.chamber_campaign(agent):
        from .community_feedback import sample_redirect
        await sample_redirect(aid)
    tracking = trials.runtime.set(action_context)
    try:
        from .activities import browser_rpc
        from .telegram_inbox import cycle as inbox_cycle
        inbox_result = None if selected and selected.delivery_ready else await inbox_cycle(aid, browser_rpc)
        if inbox_result:
            result = inbox_result
            decision["deferred_selection"] = decision.get("selected")
            decision["selected"] = {"kind": "inbox_reply", "reason": "优先回应主动来信"}
            selected = None
        elif selected:
            if selected.kind == "publication_reconcile":
                from .publication_recovery import request

                with tx(aid) as c:
                    result = request(c, aid, selected.key.removeprefix("reconcile:"))
            elif selected.task_id:
                result = await run_work(aid, selected, plan_reference)
            elif selected.kind == "audience":
                from .owned_audience import inspect
                result = await inspect(aid, selected.target_id)
            elif selected.kind == "publish":
                from .activities import _publication_cycle

                result = await _publication_cycle(
                    {**args, "operating_plan": plan_reference, "target_id": selected.target_id}
                )
            elif selected.kind == "content":
                from .content_backlog import prepare_one

                result = (await prepare_one(aid, operating_plan=plan_reference)) or {
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

                result = (await asyncio.wait_for(discover_communities(aid), timeout=610 if trials.chamber_campaign(agent) else 600)) or {"delay": 300}
            elif selected.kind == "community_progress":
                from .activities import browser_rpc
                from .community_runtime import community_cycle

                result = (await community_cycle(aid, browser_rpc, selection=selected.community_operation)) or {"delay": 300}
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
    finally:
        trials.runtime.reset(tracking)
    def finish():
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
            open_ids = {
                str(r["id"])
                for r in all_rows(
                    c,
                    "SELECT id FROM lead.tasks WHERE type LIKE 'operating.%%' AND state IN ('planned','running','failed')",
                )
            }
            state["lanes"] = {
                k: v
                for k, v in state["lanes"].items()
                if k in open_ids
                or ":" in k
                or k in {"account", "kits", "community_discovery", "community_progress"}
            }
            # Independent ready work runs on the next product timer, even when the
            # selected lane returned a long account/research backoff.
            delay = min(300, max(5, result.get("delay", 300)))
            if len(choices) > 1:
                delay = min(delay, TICK_SECONDS)
            if not choices:
                # An event can wake the supervisor just before an independent lane is due.
                # Don't turn that short remaining wait into a full five-minute idle period.
                clock = datetime.now(timezone.utc)
                pending = [datetime.fromisoformat(lane["next_at"]) for lane in state["lanes"].values() if lane.get("next_at")]
                if trials.chamber_campaign(current):
                    from .community_schedule import deadlines
                    pending.extend(at for at in deadlines(c, current, clock).values() if at)
                delay = min([delay] + [max(5, int((due-clock).total_seconds())+1) for due in pending if due>clock])
            state["next_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat()
            from .community_progress import capture
            state["business_progress"] = capture(c, aid, current)
            state["last_result"] = result
            c.execute(
                "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
                (j({"operating_kernel": state}), aid),
            )
            c.execute(
                "UPDATE lead.tasks SET state='succeeded',completed_at=now(),result=%s WHERE id=%s",
                (j({**state, "cycle_result": {**result, "delay": delay}}), tid),
            )
            current["onboarding_state"]["operating_kernel"] = state
            latest_business = one(
                c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
            )
            from .operating_plans import sync
            sync(c, aid, current, latest_business)
        return {
            **result,
            "delay": delay,
            "decision_id": tid,
            "selected_work": selected.kind if selected else None,
        }

    return await run_db(finish)


def status(c, agent):
    value = agent["onboarding_state"].get("operating_kernel", {})
    queued = all_rows(
        c,
        "SELECT id,type,state,input,result,scheduled_at FROM lead.tasks WHERE type LIKE 'operating.%%' AND type<>'operating.decision' AND state IN ('planned','running','failed') ORDER BY created_at LIMIT 12",
    )
    return {
        "engine": "value_budget_risk_v2",
        "decision": value,
        "business_progress": agent["onboarding_state"].get("community_progress", {}),
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
