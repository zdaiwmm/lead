"""Business-scoped acquisition decisions shared by plans, trials and work selection."""

from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field

from .db import j, one
from .scope import current_tenant

Source = Literal["demand", "owned", "partner", "referral"]
Layer = Literal["audience", "distribution", "value", "conversion"]
SOURCE_NAMES = {
    "demand": "主动发现需求",
    "owned": "自主分发",
    "partner": "伙伴分发",
    "referral": "用户推荐或协作",
}
LAYER_NAMES = {
    "audience": "人群与需求",
    "distribution": "触达与分发",
    "value": "价值表达",
    "conversion": "首次转化",
}
WORK = {
    "audience": ("operating.opportunities", "community_discovery"),
    "distribution": ("operating.validate", "community_progress", "publish"),
    "value": ("content", "operating.strategy"),
    "conversion": ("operating.journey", "operating.strategy"),
}


class StrategyDecision(BaseModel):
    source: Source
    trigger: str = Field(min_length=1, max_length=200)
    alternative: str = Field(min_length=1, max_length=200)
    promise: str = Field(min_length=1, max_length=200)
    proof: str = Field(min_length=1, max_length=200)
    first_step: str = Field(min_length=1, max_length=200)
    test_layer: Layer
    quality_check: str = Field(min_length=1, max_length=200)


def current(c, agent, business, goal, mode):
    state = agent["onboarding_state"]
    prefs = state.get("plan_preferences_revision", 0)
    contract_revision = state.get("operating_contract", {}).get("revision", 0)
    row = one(
        c,
        """SELECT id,result FROM lead.tasks WHERE type='operating.strategy'
        AND state='succeeded' AND result->>'status'='hypotheses'
        AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
        AND result->>'contract_revision'=%s
        AND result->'alternatives'->0 ? 'decision'
        ORDER BY completed_at DESC,id DESC LIMIT 1""",
        (str(business["id"]), str(agent["config_revision"]), str(contract_revision)),
    )
    option = row["result"]["alternatives"][0] if row else {}
    decision = option.get("decision", {})
    criteria = state.get("acquisition_criteria") or {}
    source = decision.get("source", "demand")
    layer = decision.get("test_layer", "audience")
    reason = (
        row["result"]["bottleneck"] if row else "先核实一个目标人群的真实任务与需求来源"
    )
    action = option.get("next_action", "在公开来源查证需求，保存原话、场景和引用")
    event = next(
        (e for e in mode.get("events", []) if e["name"] == mode["objective_event"]),
        None,
    )
    definition_current = event and criteria.get("event_key") == [
        event["connection_id"],
        event["name"],
        event["revision"],
    ]
    # Missing site coverage is a measurement dependency. It cannot refute a channel.
    return {
        "version": f"{business['id']}/{agent['config_revision']}/{prefs}/{contract_revision}/{row['id'] if row else 'discovery'}",
        "status": "待验证假设" if row else "待核验需求",
        "audience": option.get("audience") or goal["audience"] or "待核验",
        "trigger": decision.get("trigger"),
        "alternative": decision.get("alternative"),
        "source": source,
        "source_label": SOURCE_NAMES[source],
        "promise": decision.get("promise"),
        "proof": decision.get("proof"),
        "entry": business.get("product_url"),
        "first_step": decision.get("first_step"),
        "objective_event": mode["objective_event"],
        "quality_policy": criteria.get("quality"),
        "quality_check": criteria.get("quality") or decision.get("quality_check"),
        "first_definition": criteria.get("first_definition")
        if definition_current
        else None,
        "cost_ceiling": criteria.get("cost_ceiling"),
        "test_layer": layer,
        "layer_label": LAYER_NAMES[layer],
        "review_signal": (row["result"].get("review_signal") if row else None),
        "bottleneck": reason,
        "next_action": action,
        "validation": option.get("validation"),
        "stop_condition": option.get("stop_condition"),
        "evidence_task_ids": option.get("evidence_task_ids", []),
        "measurement": "目标事件已接通"
        if mode["objective_ready"]
        else "目标转化待核验",
    }


def target_results(c, target, start, end, experiment_id=None):
    """Report only source-linked live receipts for the frozen goal definition."""
    if not target or not target.get("ready"):
        return {"events": None, "new_users": None}
    counts = one(
        c,
        """SELECT count(*) AS events,
        count(DISTINCT r.user_key) FILTER(WHERE r.trust_level='server_verified') AS identified,
        count(*) FILTER(WHERE r.trust_level<>'server_verified' OR r.user_key IS NULL) AS unverified
        FROM lead.pixel_receipts r JOIN lead.pixel_touchpoints p ON p.id=r.touchpoint_id
        JOIN lead.tracking_links l ON l.id=p.tracking_link_id
        WHERE r.connection_id=%s AND r.event_name=%s AND r.definition_revision=%s
        AND r.counted AND r.scope_key='live' AND r.occurred_at>=%s AND r.occurred_at<%s
        AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM lead.tasks t
          WHERE t.input->'operating_plan'->>'experiment_id'=%s
          AND t.input->>'content_id'=l.content_id::text AND t.type LIKE '%%.publish'))""",
        (
            target["connection_id"],
            target["name"],
            target["revision"],
            start,
            end,
            experiment_id,
            experiment_id,
        ),
    )
    # A first-event contract is supplied by the product owner. Ordinary purchase
    # or activation receipts alone cannot distinguish an existing customer.
    return {
        "events": counts["events"] if counts["events"] else None,
        "new_users": counts["identified"]
        if counts["events"]
        and not counts["unverified"]
        and target.get("first_definition")
        and target.get("method") == "server"
        else None,
    }


def persist(c, aid, strategy):
    # Stable identity across midnight, week boundaries, restarts and worker retries.
    key = str(uuid5(NAMESPACE_URL, f"{aid}/acquisition-strategy/{strategy['version']}"))
    body = {
        k: v for k, v in strategy.items() if k not in ("results_30d", "measurement")
    }
    c.execute(
        """INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation)
        VALUES(%s,%s,%s,'observation',%s,%s,'system_observed') ON CONFLICT(id) DO NOTHING""",
        (
            key,
            current_tenant(),
            aid,
            j({"type": "acquisition_strategy", **body}),
            j(body["evidence_task_ids"]),
        ),
    )


def stored(c, agent, business):
    state = agent["onboarding_state"]
    prefix = f"{business['id']}/{agent['config_revision']}/{state.get('plan_preferences_revision', 0)}/{state.get('operating_contract', {}).get('revision', 0)}/"
    row = one(
        c,
        """SELECT body FROM lead.memory_entries WHERE kind='observation'
        AND body->>'type'='acquisition_strategy' AND starts_with(body->>'version',%s)
        AND deleted_at IS NULL ORDER BY created_at DESC,id DESC LIMIT 1""",
        (prefix,),
    )
    return row["body"] if row else None


def review_signal(mode):
    """Finite evidence milestones; a clock tick never suffices to commission a strategy."""
    goal = next((e for e in mode["events"] if e["name"] == mode["objective_event"]), {})
    if goal.get("ready") and goal.get("attributed_count_30d"):
        return "attributed_target"
    if mode["clicks"]["filtered_clicks_30d"]:
        return "observed_traffic"
    return "demand_research"
