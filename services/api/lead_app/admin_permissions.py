"""Per-request capability decisions backed by published workspace policy and RLS."""

import re
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import all_rows, one, tx
from .scope import principal

CAPABILITIES = {
    "agents.create": "创建 Agent",
    "agents.edit": "编辑业务与资料",
    "agents.chat": "Agent 对话与重试",
    "agents.control": "启动、暂停与归档",
    "accounts.manage": "账号登录与授权",
    "analytics.manage": "网站与服务端接入",
    "admin.agents": "批量管理 Agent",
    "admin.budgets": "管理 Agent 额度",
    "admin.strategies": "维护渠道策略",
    "admin.channels": "维护渠道规则",
    "admin.experiences": "审核运营经验",
    "admin.attribution": "维护数据与归因",
    "admin.onboarding": "维护创建引导",
    "admin.models": "配置模型与工具",
    "admin.brand": "维护品牌知识",
    "admin.runtime": "配置调度与异常",
    "admin.notifications": "配置站内通知",
    "admin.pricing": "维护模型定价",
}
OPERATOR_DEFAULTS = [
    "agents.create",
    "agents.edit",
    "agents.chat",
    "agents.control",
    "accounts.manage",
    "analytics.manage",
]
KIND_CAPS = {
    "budget": "admin.budgets",
    "strategy": "admin.strategies",
    "channel_rule": "admin.channels",
    "experience": "admin.experiences",
    "attribution": "admin.attribution",
    "onboarding": "admin.onboarding",
    "models": "admin.models",
    "model_catalog": "admin.models",
    "brand": "admin.brand",
    "runtime": "admin.runtime",
    "notifications": "admin.notifications",
    "pricing": "admin.pricing",
}


class MemberPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issuer: str = Field(min_length=1, max_length=2000)
    subject: str = Field(min_length=1, max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=30)
    agent_ids: list[UUID] | None = Field(default=None, max_length=1000)

    @field_validator("capabilities")
    @classmethod
    def capabilities_known(cls, value):
        if len(set(value)) != len(value) or not set(value) <= set(CAPABILITIES):
            raise ValueError("权限选项无效或重复")
        return value


class MemberAccessSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    members: list[MemberPolicy] = Field(default_factory=list, max_length=300)


def resolve(identity):
    if identity["role"] == "owner":
        return {
            "capabilities": list(CAPABILITIES),
            "agent_ids": None,
            "access_resolved": True,
        }
    with tx() as c:
        row = one(
            c,
            "SELECT body FROM lead.admin_configs WHERE config_key='member-access/default'",
        )
    body = row["body"] if row and row["body"] else {"members": []}
    policy = next(
        (
            m
            for m in body["members"]
            if m["issuer"] == identity["issuer"] and m["subject"] == identity["subject"]
        ),
        None,
    )
    caps = policy["capabilities"] if policy else OPERATOR_DEFAULTS
    return {
        "capabilities": caps if identity["role"] == "operator" else [],
        "agent_ids": policy["agent_ids"] if policy else None,
        "access_resolved": True,
    }


def require(capability, *, workspace=False):
    who = principal()
    if who and who.get("role") == "owner":
        return
    if (
        not who
        or who.get("role") != "operator"
        or capability not in who.get("capabilities", [])
    ):
        raise HTTPException(403, "当前成员没有此项操作权限")
    if workspace and who.get("agent_ids") is not None:
        raise HTTPException(403, "此操作会影响整个工作室，需要不限 Agent 范围的权限")


def require_config(key):
    from .admin_config import parse_key

    kind, aid, _, _ = parse_key(key)
    who = principal()
    if who is None:
        return  # trusted internal runtime; HTTP handlers always have a principal
    if kind not in KIND_CAPS:
        if who["role"] != "owner":
            raise HTTPException(403, "成员权限与外部通知凭据仅由工作室所有者管理")
    else:
        require(KIND_CAPS[kind], workspace=not aid)


def validate_members(c, data):
    identities = set()
    for entry in data["members"]:
        identity = (entry["issuer"], entry["subject"])
        if identity in identities:
            raise ValueError("同一成员不能重复配置")
        identities.add(identity)
        member = one(
            c,
            "SELECT role,status FROM lead.memberships WHERE oidc_issuer=%s AND oidc_subject=%s",
            identity,
        )
        if not member or member["status"] != "active" or member["role"] == "owner":
            raise ValueError(
                "只能细分当前工作室有效的运营或查看成员权限，所有者保留完整权限"
            )
        if member["role"] == "viewer" and entry["capabilities"]:
            raise ValueError("查看成员始终只读；需要操作权限请先调整为运营成员")
        if entry["agent_ids"] is not None:
            ids = entry["agent_ids"]
            if len(set(ids)) != len(ids) or len(
                all_rows(
                    c,
                    "SELECT id FROM lead.agents WHERE id=ANY(%s::uuid[]) AND desired_state<>'archived'",
                    (ids,),
                )
            ) != len(ids):
                raise ValueError("Agent 范围必须属于当前工作室且未归档")


def workbench_permission(method, path):
    if method in ("HEAD", "OPTIONS"):
        return None
    if path.endswith("/browser/screenshot"):
        return "accounts.manage"
    if method == "GET":
        if path.endswith("/export"):
            return "agents.edit"
        if path.endswith("/analytics"):
            return "analytics.manage"  # includes the public website write key
        return None
    if re.fullmatch(r"/api/agents(?:/setup)?", path):
        return "agents.create"
    if not path.startswith("/api/agents/"):
        return None
    suffix = path.split("/", 4)[-1]
    if suffix == "budget":
        return "admin.budgets"
    if suffix in ("messages", "retry"):
        return "agents.chat"
    if suffix == "control" or suffix.endswith("/control"):
        return "agents.control"
    if suffix.startswith(("analytics", "tracking-links")):
        return "analytics.manage"
    if suffix.startswith(
        (
            "connect",
            "takeover",
            "browser/",
            "accounts/",
            "targets/",
            "telegram/",
            "identity/check",
            "lab/",
            "cards/",
        )
    ):
        return "accounts.manage"
    return "agents.edit"
