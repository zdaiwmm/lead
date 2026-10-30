"""Published rules only narrow delegated operations; attribution is pinned per visit."""

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import all_rows, one


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChannelRule(StrictSettings):
    enabled: bool = True
    allow_publishing: bool = True
    allow_community_growth: bool = True
    formats: list[Literal["text", "image", "video"]] = Field(
        default_factory=lambda: ["text", "image", "video"], max_length=3
    )
    daily_publish_limit: int | None = Field(default=None, strict=True, ge=0, le=100)

    @field_validator("formats")
    @classmethod
    def distinct_formats(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("内容格式不能重复")
        return value


class ModelCatalogSettings(StrictSettings):
    openai_models: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("openai_models")
    @classmethod
    def valid_models(cls, value):
        if len(set(value)) != len(value) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name) for name in value
        ):
            raise ValueError(
                "模型名称需要唯一且使用接口中的原始模型 ID，不包含服务前缀"
            )
        return value


class AttributionSettings(StrictSettings):
    objective: Literal["lead", "signup", "trial", "purchase"] = "signup"
    objective_definition: str = Field(
        default="由服务端确认用户完成注册，不把点击或网页浏览当作注册。",
        min_length=1,
        max_length=2000,
    )
    attribution_window_days: int = Field(default=30, strict=True, ge=1, le=30)
    engagement_seconds: int = Field(default=10, strict=True, ge=10, le=120)
    freshness_hours: int = Field(default=24, strict=True, ge=1, le=168)


class ExperienceSettings(StrictSettings):
    title: str = Field(default="新的渠道经验", min_length=1, max_length=160)
    platform: str = "telegram"
    target_id: UUID | None = None
    business_profile_id: UUID | None = None
    plan_id: str = Field(default="", max_length=120)
    applicable_conditions: str = Field(default="", max_length=3000)
    observation: str = Field(default="", max_length=6000)
    proposed_change: str = Field(default="", max_length=4000)
    evidence_action_ids: list[UUID] = Field(default_factory=list, max_length=20)
    review_status: Literal["pending", "approved", "rejected", "retired"] = "pending"

    @field_validator("evidence_action_ids")
    @classmethod
    def distinct_evidence(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("依据记录不能重复")
        return value


def channel_policy(c, aid, platform, target=None):
    from .admin_config import head

    result = ChannelRule().model_dump(mode="json")
    versions = []
    for key in [
        f"channel-rules/{platform}",
        f"channel-rules/{platform}/{aid}",
        *([f"channel-rules/{platform}/{aid}/{target}"] if target else []),
    ]:
        row = head(c, key)
        if not row or row["body"] is None:
            continue
        body = row["body"]
        for field in ("enabled", "allow_publishing", "allow_community_growth"):
            result[field] = result[field] and body[field]
        result["formats"] = [
            value for value in result["formats"] if value in body["formats"]
        ]
        limit = body["daily_publish_limit"]
        if limit is not None:
            result["daily_publish_limit"] = (
                min(result["daily_publish_limit"], limit)
                if result["daily_publish_limit"] is not None
                else limit
            )
        versions.append(f"{key}@{row['published_version']}")
    return result, versions


def enforce_channel(
    c,
    aid,
    platform,
    target=None,
    *,
    operation="publish",
    content_format="text",
    action_id=None,
):
    if platform == "lab":
        return
    policy, _ = channel_policy(c, aid, platform, target)
    if not policy["enabled"]:
        raise ValueError("此渠道已由后台停用")
    if operation == "community" and not policy["allow_community_growth"]:
        raise ValueError("此渠道的社群运营已由后台停用")
    if operation != "publish":
        return
    if not policy["allow_publishing"] or content_format not in policy["formats"]:
        raise ValueError("后台规则未允许此渠道的发布操作或内容格式")
    if target and policy["daily_publish_limit"] is not None:
        used = one(
            c,
            "SELECT count(*) AS n FROM lead.actions WHERE target_id=%s AND (%s::uuid IS NULL OR id<>%s) AND state IN ('dispatching','unknown','confirmed') AND created_at>now()-interval '24 hours'",
            (target, action_id, action_id),
        )["n"]
        if used >= policy["daily_publish_limit"]:
            raise ValueError("此目标已达到后台设置的过去 24 小时发布上限")


def attribution(c, aid=None):
    from .admin_settings import effective

    if aid is None:
        aid = (
            one(c, "SELECT current_setting('app.agent_id',true) AS aid")["aid"] or None
        )
    body, version = effective(c, "attribution", aid)
    if version == "attribution/builtin@0" and aid:
        business = one(
            c,
            "SELECT facts FROM lead.business_profiles WHERE agent_id=%s ORDER BY revision DESC LIMIT 1",
            (aid,),
        )
        if business and business["facts"].get("conversion_goal") in {
            "lead",
            "signup",
            "trial",
            "purchase",
        }:
            body["objective"] = business["facts"]["conversion_goal"]
    return body, version


def legacy_attribution(snapshot):
    return {**AttributionSettings().model_dump(mode="json"), **(snapshot or {})}


def channel_rule_published(c, platform, aid=None, target=None):
    from .db import emit, record

    for agent in all_rows(
        c,
        "SELECT DISTINCT a.id FROM lead.agents a LEFT JOIN lead.social_accounts s ON s.agent_id=a.id WHERE a.desired_state<>'archived' AND (%s::uuid IS NULL OR a.id=%s) AND (s.platform=%s OR a.id=%s)",
        (aid, aid, platform, aid),
    ):
        agent_id = str(agent["id"])
        policy, _ = channel_policy(c, agent_id, platform, target)
        if not policy["enabled"] or not policy["allow_publishing"]:
            c.execute(
                """UPDATE lead.tasks t SET state='canceled',completed_at=now() WHERE t.agent_id=%s AND t.type=%s AND t.state IN ('planned','queued')
              AND (%s::uuid IS NULL OR t.input->>'target_id'=%s) AND NOT EXISTS(SELECT 1 FROM lead.actions a WHERE a.task_id=t.id AND a.state IN ('dispatching','unknown','confirmed'))""",
                (
                    agent_id,
                    platform + ".publish",
                    target,
                    str(target) if target else None,
                ),
            )
        record(
            c, agent_id, "渠道后台规则已更新", platform + "，后续操作按当前规则核对。"
        )
        emit(c, agent_id, "supervisor.wake")


def validate_experience(c, aid, data):
    from .domain import PLATFORMS

    if data["platform"] not in {p[0] for p in PLATFORMS}:
        raise ValueError("请选择有效的渠道")
    if data["target_id"] and not one(
        c,
        "SELECT t.id FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s AND t.agent_id=%s AND s.platform=%s",
        (data["target_id"], aid, data["platform"]),
    ):
        raise ValueError("经验的账号目标不属于此 Agent 与渠道")
    profile = one(
        c,
        "SELECT id FROM lead.business_profiles WHERE agent_id=%s ORDER BY revision DESC LIMIT 1",
        (aid,),
    )
    if not profile:
        raise ValueError("请先完善此 Agent 的业务档案")
    data["business_profile_id"] = data["business_profile_id"] or str(profile["id"])
    if not one(
        c,
        "SELECT id FROM lead.business_profiles WHERE id=%s AND agent_id=%s",
        (data["business_profile_id"], aid),
    ):
        raise ValueError("经验业务档案不属于此 Agent")
    if data["evidence_action_ids"]:
        rows = all_rows(
            c,
            """SELECT a.id FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id
          JOIN lead.content_items ci ON ci.id=a.content_id JOIN lead.plans p ON p.id=ci.plan_id
          WHERE a.id=ANY(%s::uuid[]) AND a.agent_id=%s AND a.state='confirmed' AND a.intent->>'platform'=%s
          AND (%s::uuid IS NULL OR a.target_id=%s) AND p.business_profile_id=%s
          AND (%s='' OR p.body->'operating_strategy'->>'plan_id'=%s)
          AND coalesce(t.input->>'bootstrap_verification','false')<>'true'""",
            (
                data["evidence_action_ids"],
                aid,
                data["platform"],
                data["target_id"],
                data["target_id"],
                data["business_profile_id"],
                data["plan_id"],
                data["plan_id"],
            ),
        )
        if len(rows) != len(data["evidence_action_ids"]):
            raise ValueError(
                "依据必须来自此业务、渠道和目标下已确认的真实发布，连接测试不作为效果依据"
            )
    if data["review_status"] == "approved" and (
        not data["applicable_conditions"]
        or not data["observation"]
        or not data["proposed_change"]
    ):
        raise ValueError("审核通过前请补齐适用条件、观察与建议做法")
    return data
