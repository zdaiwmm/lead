"""Schemas and runtime resolution for phase-two management settings."""

import re
from datetime import date, datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .admin_controls import (
    AttributionSettings,
    ChannelRule,
    ExperienceSettings,
    ModelCatalogSettings,
)
from .config import env, model_config, model_reference, resolve_model


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolSettings(SettingsModel):
    public_web: bool = True
    advanced_research: bool = True
    community_research: bool = True
    media_creation: bool = True


class TaskModels(SettingsModel):
    chat: str = ""
    planning: str = ""
    research: str = ""


class ModelSettings(SettingsModel):
    default_model: str = Field(min_length=1, max_length=100)
    task_models: TaskModels = Field(default_factory=TaskModels)
    timeout_seconds: int = Field(default=90, strict=True, ge=15, le=120)
    max_output_tokens: int = Field(default=4000, strict=True, ge=256, le=8000)
    max_model_requests: int = Field(default=10, strict=True, ge=1, le=10)
    max_tool_calls: int = Field(default=12, strict=True, ge=0, le=20)
    tools: ToolSettings = Field(default_factory=ToolSettings)


class KnowledgeItem(SettingsModel):
    id: UUID
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=20000)
    source: str = Field(default="用户提供", max_length=2000)
    status: Literal["pending", "approved", "disabled"] = "pending"
    valid_until: date | None = None
    tags: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, values):
        if any(not value.strip() or len(value) > 100 for value in values):
            raise ValueError("标签需要 1–100 个字符")
        return values

    @field_validator("source")
    @classmethod
    def source_is_not_a_credential_url(cls, value):
        if "://" in value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("来源网址需要是无账号密码的 http 或 https 地址")
        return value


class BrandSettings(SettingsModel):
    name: str = Field(default="", max_length=100)
    voice: str = Field(
        default="自然、清楚、专业，以可核对的事实表达。", max_length=2000
    )
    language: str = Field(default="跟随业务和渠道受众", max_length=100)
    audience: str = Field(default="", max_length=2000)
    positioning: str = Field(default="", max_length=4000)
    facts: list[str] = Field(default_factory=list, max_length=40)
    forbidden_phrases: list[str] = Field(default_factory=list, max_length=50)
    claims_to_avoid: list[str] = Field(default_factory=list, max_length=30)
    asset_guidance: str = Field(default="", max_length=4000)
    documents: list[KnowledgeItem] = Field(default_factory=list, max_length=40)


class WorkHours(SettingsModel):
    enabled: bool = False
    timezone: str = "Asia/Shanghai"
    weekdays: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4, 5, 6], min_length=1, max_length=7
    )
    start: str = "09:00"
    end: str = "21:00"

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("请选择有效的时区") from None
        return value

    @field_validator("start", "end")
    @classmethod
    def valid_clock(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("时间需要使用 HH:MM 格式")
        return value

    @field_validator("weekdays")
    @classmethod
    def valid_days(cls, value):
        if len(set(value)) != len(value) or any(
            type(d) is not int or not 0 <= d <= 6 for d in value
        ):
            raise ValueError("请选择不重复的有效星期")
        return sorted(value)


class RuntimeSettings(SettingsModel):
    work_hours: WorkHours = Field(default_factory=WorkHours)
    max_concurrent_agents: int = Field(default=3, strict=True, ge=1, le=12)
    retry_attempts: int = Field(default=3, strict=True, ge=1, le=10)
    retry_interval_seconds: int = Field(default=30, strict=True, ge=5, le=600)
    pause_after_failures: int = Field(default=5, strict=True, ge=1, le=20)


class QuietHours(WorkHours):
    start: str = "23:00"
    end: str = "08:00"


class NotificationSettings(SettingsModel):
    enabled: bool = True
    categories: list[
        Literal[
            "budget_warning",
            "budget_exhausted",
            "login_expired",
            "repeated_failures",
            "uncertain_action",
            "knowledge_expired",
            "measurement_stale",
        ]
    ] = Field(
        default_factory=lambda: [
            "budget_warning",
            "budget_exhausted",
            "login_expired",
            "repeated_failures",
            "uncertain_action",
            "knowledge_expired",
            "measurement_stale",
        ]
    )
    budget_warning_percent: int = Field(default=80, strict=True, ge=1, le=99)
    recipient_roles: list[Literal["owner", "operator", "viewer"]] = Field(
        default_factory=lambda: ["owner"]
    )
    recipient_subjects: list[str] = Field(default_factory=list, max_length=100)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    urgent_during_quiet: bool = True
    recurrence_cooldown_minutes: int = Field(default=30, strict=True, ge=1, le=1440)


SCHEMAS = {
    "model_catalog": ModelCatalogSettings,
    "channel_rule": ChannelRule,
    "attribution": AttributionSettings,
    "experience": ExperienceSettings,
    "models": ModelSettings,
    "brand": BrandSettings,
    "runtime": RuntimeSettings,
    "notifications": NotificationSettings,
}


def model_catalog(c=None):
    from .db import one, tx

    if c is None:
        with tx() as connection:
            return model_catalog(connection)
    configured = one(
        c,
        "SELECT body FROM lead.admin_configs WHERE config_key='model-catalog/default'",
    )
    managed = (
        configured["body"]["openai_models"] if configured and configured["body"] else []
    )
    default = model_reference(model_config())
    return list(
        dict.fromkeys(
            [
                default,
                model_config("deepseek")["model"],
                "openai:" + model_config("openai")["model"],
                *["openai:" + name for name in managed],
                *[
                    "openai:" + name.strip()
                    for name in (env().get("OPENAI_ALLOWED_MODELS") or "").split(",")
                    if name.strip()
                ],
                *[
                    name.strip()
                    for name in (env().get("LEAD_ALLOWED_MODELS") or "").split(",")
                    if name.strip()
                ],
            ]
        )
    )


def defaults(kind):
    if kind == "models":
        return ModelSettings(default_model=model_reference(model_config())).model_dump(
            mode="json"
        )
    return SCHEMAS[kind]().model_dump(mode="json")


def validate(kind, data, c=None):
    if kind == "models":
        allowed = model_catalog(c) if c is not None else model_catalog()
        if any(
            model and model not in allowed
            for model in [data["default_model"], *data["task_models"].values()]
        ):
            raise ValueError("模型不在此服务允许使用的列表中")
        for name in [data["default_model"], *data["task_models"].values()]:
            if name:
                resolve_model(name)
    elif kind == "brand":
        ids = [doc["id"] for doc in data["documents"]]
        if len(set(ids)) != len(ids):
            raise ValueError("知识条目标识不能重复")
        for key in ("facts", "forbidden_phrases", "claims_to_avoid"):
            if any(not value.strip() or len(value) > 2000 for value in data[key]):
                raise ValueError("品牌规则条目不能为空或超过 2000 字")
    elif kind == "runtime":
        hours = data["work_hours"]
        if hours["enabled"] and hours["start"] == hours["end"]:
            raise ValueError("工作时段的开始和结束不能相同；全天运行请关闭时段限制")
    elif kind == "notifications":
        if (
            data["quiet_hours"]["enabled"]
            and data["quiet_hours"]["start"] == data["quiet_hours"]["end"]
        ):
            raise ValueError("免打扰的开始与结束不能相同")
        if (
            data["enabled"]
            and not data["recipient_roles"]
            and not data["recipient_subjects"]
        ):
            raise ValueError("启用通知时至少选择一种接收范围")
        for key in ("categories", "recipient_roles", "recipient_subjects"):
            if len(data[key]) != len(set(data[key])):
                raise ValueError("通知选项不能重复")
        if any(
            not subject or len(subject) > 500 for subject in data["recipient_subjects"]
        ):
            raise ValueError("成员编号无效")


def effective(c, kind, aid=None):
    from .admin_config import head

    for key in ([f"{kind}/{aid}"] if aid else []) + [f"{kind}/default"]:
        row = head(c, key)
        if row and row["body"] is not None:
            return row["body"], f"{key}@{row['published_version']}"
    return defaults(kind), f"{kind}/builtin@0"


def for_agent(aid, kind):
    from .db import require_agent, tx

    with tx(aid) as c:
        require_agent(c, aid)
        return effective(c, kind, aid)


def task_connection(c, aid, purpose="planning"):
    settings, version = effective(c, "models", aid)
    reference = settings["task_models"].get(purpose) or settings["default_model"]
    return resolve_model(reference), reference, version


def tool_enabled(aid, tool):
    return for_agent(aid, "models")[0]["tools"][tool]


def brand_context(c, aid, now=None, query=""):
    data, version = effective(c, "brand", aid)
    today = (
        (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai")).date()
    )
    documents = [
        doc
        for doc in data["documents"]
        if doc["status"] == "approved"
        and (not doc["valid_until"] or date.fromisoformat(doc["valid_until"]) >= today)
    ]
    if query:
        terms = set(
            re.findall(r"[a-z0-9_]{3,40}|[\u3400-\u9fff]{2,16}", query.lower()[:20000])
        )

        def score(doc):
            title = (doc["title"] + " " + " ".join(doc["tags"])).lower()
            text = doc["content"].lower()
            return sum(3 * (term in title) + (term in text) for term in terms)

        documents = sorted(documents, key=score, reverse=True)
    documents = [{**doc, "content": doc["content"][:6000]} for doc in documents[:6]]
    return {
        **data,
        "version": version,
        "documents": documents,
        "trust": "owner_provided",
        "scope": "品牌和业务资料，不授予工具或账号权限",
    }


def validate_brand_content(c, aid, text):
    data, _ = effective(c, "brand", aid)
    if any(
        phrase.casefold() in text.casefold() for phrase in data["forbidden_phrases"]
    ):
        raise ValueError("内容包含品牌禁用表达，请调整后再发布")


def content_context(aid, query=""):
    from .db import tx

    with tx(aid) as c:
        return brand_context(c, aid, query=query)
