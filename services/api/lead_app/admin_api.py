"""Authenticated management endpoints. All reads remain workspace scoped."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import admin_config as configs
from .budget import configuration
from .db import all_rows, tx
from .scope import principal

router = APIRouter(prefix="/api")


def require_owner():
    who = principal()
    if not who or who.get("role") != "owner":
        raise HTTPException(403, "仅工作室所有者可以修改管理配置")


@router.get("/admin/content-library")
def content_library():
    from .admin_content import library

    with tx() as c:
        return library(c)


class ContentInitialization(BaseModel):
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_versions: dict[str, Annotated[int, Field(strict=True, ge=0)]] = Field(
        min_length=1, max_length=100
    )


@router.post("/admin/content-library/initialize", dependencies=[Depends(require_owner)])
def initialize_content(body: ContentInitialization):
    from .admin_content import initialize

    with tx() as c:
        return initialize(c, body.expected_versions, body.content_digest)


@router.get("/admin/readiness")
def readiness():
    from .admin_readiness import overview

    with tx() as c:
        return overview(c)


@router.get("/admin/model-usage")
def usage_by_model(aid: UUID | None = None, include_archived: bool = False):
    from .metering import model_usage

    with tx() as c:
        if aid:
            configs.check_scope(c, "models/" + str(aid))
        return {
            "period": "30d",
            "reset_timezone": "UTC",
            "include_archived": include_archived,
            "by_model": model_usage(c, aid, active_only=not include_archived),
        }


class ConfigChange(BaseModel):
    key: str = Field(max_length=220)
    expected_version: int = Field(ge=0)
    body: dict = Field(default_factory=dict)
    action: Literal["draft", "publish", "rollback"] = "publish"
    rollback_version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=1000)
    reset_to_default: bool = False


@router.get("/admin/config")
def config(key: str):
    with tx() as c:
        return configs.read(c, key)


@router.post("/admin/config", dependencies=[Depends(require_owner)])
def change_config(body: ConfigChange):
    with tx() as c:
        return configs.write(c, **body.model_dump())


@router.get("/admin/history")
def history(
    key: str | None = None,
    before: datetime | None = None,
    before_id: UUID | None = None,
    limit: int = 40,
):
    with tx() as c:
        return configs.history(c, key, before, min(max(limit, 1), 100), before_id)


@router.get("/admin/agents")
def agents():
    with tx() as c:
        rows = all_rows(
            c,
            """
            SELECT a.id,a.name,a.desired_state,a.health_state,a.onboarding_state,
              coalesce(b.spent_units,0) AS spent,coalesce(b.reserved_units,0) AS reserved,
              b.ends_at AS resets_at,
              (SELECT count(*) FROM lead.social_accounts s WHERE s.agent_id=a.id AND s.state='connected') AS connected_accounts
            FROM lead.agents a LEFT JOIN LATERAL (
              SELECT * FROM lead.budget_periods b WHERE b.agent_id=a.id AND b.unit='tokens'
              AND b.starts_at<=now() AND b.ends_at>now() LIMIT 1
            ) b ON true WHERE a.desired_state<>'archived' ORDER BY a.created_at
        """,
        )
        for row in rows:
            aid = str(row["id"])
            row["setup_pending"] = row.pop("onboarding_state").get(
                "setup_pending", False
            )
            row["budget"] = configuration(c, aid)
            row["config"] = configs.read(c, "budgets/" + aid)
            row["remaining"] = max(
                0, row["budget"]["daily_token_limit"] - row["spent"] - row["reserved"]
            )
        return rows


@router.get("/admin/agent-directory")
def agent_directory(
    query: str = "",
    state: str = "active",
    platform: str | None = None,
    before: datetime | None = None,
    before_id: UUID | None = None,
):
    from .admin_management import directory

    with tx() as c:
        return directory(c, query[:100], state, platform, before, before_id)


class AgentControl(BaseModel):
    agents: list[UUID] = Field(min_length=1, max_length=100)
    action: Literal["pause", "archive"]
    expected_control_versions: dict[str, Annotated[int, Field(strict=True, ge=0)]]
    note: str = Field(default="", max_length=1000)


@router.post("/admin/agents/control", dependencies=[Depends(require_owner)])
def control_selected_agents(body: AgentControl):
    from .admin_management import control_agents

    with tx() as c:
        return control_agents(
            c, body.agents, body.action, body.expected_control_versions, body.note
        )


@router.get("/admin/channel-rules/effective")
def effective_channel_rules(
    platform: str, aid: UUID | None = None, target: UUID | None = None
):
    from .admin_controls import channel_policy

    if target and not aid:
        raise ValueError("选择账号目标前请先选择 Agent")
    key = (
        f"channel-rules/{platform}"
        + (f"/{aid}" if aid else "")
        + (f"/{target}" if target else "")
    )
    with tx() as c:
        configs.check_scope(c, key)
        policy, versions = channel_policy(
            c, str(aid) if aid else "", platform, str(target) if target else None
        )
        return {"policy": policy, "versions": versions}


@router.get("/admin/attribution-status")
def attribution_status(aid: UUID):
    from .admin_controls import attribution
    from .db import one, require_agent

    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        policy, version = attribution(c, str(aid))
        site = one(
            c,
            "SELECT id,site_host,state,last_received_at FROM lead.analytics_connections WHERE provider='lead_site' ORDER BY created_at DESC LIMIT 1",
        )
        server = one(
            c,
            "SELECT max(last_received_at) AS last_received_at FROM lead.analytics_credentials WHERE state='active'",
        )
        return {
            "policy": policy,
            "version": version,
            "site": site,
            "server_last_received_at": server["last_received_at"],
        }


@router.get("/admin/strategy-experiences")
def strategy_experiences(aid: UUID | None = None, platform: str | None = None):
    from .admin_experiences import listing

    with tx() as c:
        return listing(c, aid, platform)


@router.get("/admin/strategy-experiences/evidence")
def strategy_experience_evidence(aid: UUID, platform: str, target: UUID | None = None):
    from .admin_experiences import evidence_options

    with tx() as c:
        return evidence_options(c, str(aid), platform, target)


class ExperienceCapture(BaseModel):
    agent_id: UUID
    target_id: UUID


@router.post(
    "/admin/strategy-experiences/capture", dependencies=[Depends(require_owner)]
)
def capture_experience(body: ExperienceCapture):
    from .admin_experiences import capture

    with tx() as c:
        configs.check_scope(c, "strategies/telegram/" + str(body.agent_id))
        return capture(c, str(body.agent_id), str(body.target_id))


class ExperiencePromotion(BaseModel):
    key: str = Field(max_length=220)
    expected_version: int = Field(strict=True, ge=1)
    target_key: str = Field(max_length=220)
    target_version: int = Field(strict=True, ge=0)
    reusable_practice: str = Field(min_length=1, max_length=3000)
    applicable_conditions: str = Field(min_length=1, max_length=2000)


@router.post(
    "/admin/strategy-experiences/promote", dependencies=[Depends(require_owner)]
)
def promote_experience(body: ExperiencePromotion):
    from .admin_experiences import promote

    with tx() as c:
        return promote(
            c,
            body.key,
            body.expected_version,
            body.target_key,
            body.target_version,
            body.reusable_practice,
            body.applicable_conditions,
        )


class BulkBudget(BaseModel):
    agents: list[UUID] = Field(min_length=1, max_length=100)
    daily_token_limit: int | None = Field(
        default=None, strict=True, ge=0, le=configs.MAX_TOKENS
    )
    expected_versions: dict[str, int]
    note: str = Field(default="", max_length=1000)


@router.post("/admin/budgets/bulk", dependencies=[Depends(require_owner)])
def bulk_budget(body: BulkBudget):
    ids = sorted({str(a) for a in body.agents})
    if set(ids) != set(body.expected_versions):
        raise HTTPException(422, "请提供每个 Agent 的当前配置版本")
    with tx() as c:
        return [
            configs.write(
                c,
                "budgets/" + aid,
                {"daily_token_limit": body.daily_token_limit},
                body.expected_versions[aid],
                note=body.note,
            )
            for aid in ids
        ]


@router.get("/admin/strategy-options")
def strategy_options():
    from .domain import PLATFORMS

    with tx() as c:
        targets = all_rows(
            c,
            """
            SELECT t.id,t.agent_id,t.display_name,s.platform,s.display_name AS account_name
            FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id
            JOIN lead.agents a ON a.id=t.agent_id
            WHERE t.state='active' AND a.desired_state<>'archived' ORDER BY s.created_at,t.id
        """,
        )
    return {
        "platforms": [{"id": p[0], "name": p[1]} for p in PLATFORMS],
        "targets": targets,
    }


@router.get("/onboarding-template")
def onboarding_template():
    with tx() as c:
        return configs.onboarding_snapshot(c)


@router.get("/admin/strategy-evidence/{aid}")
def strategy_evidence(aid: UUID):
    from .operating_playbooks import get_playbooks

    return get_playbooks(aid)


@router.get("/admin/model-options")
def model_options():
    from .admin_settings import model_catalog
    from .config import model_config
    from .hybrid_research import available

    connection = model_config()
    providers = [
        {
            "id": provider,
            "name": name,
            "credential_configured": bool(model_config(provider)["key"]),
            "default_model": model_config(provider)["model"],
        }
        for provider, name in (("deepseek", "DeepSeek"), ("openai", "OpenAI"))
    ]
    return {
        "models": model_catalog(),
        "credential_configured": bool(connection["key"]),
        "provider": connection["provider"],
        "providers": providers,
        "advanced_research_available": available(),
        "credentials_managed_by_server": True,
    }


class ModelCheck(BaseModel):
    models: list[str] = Field(default_factory=list, max_length=4)


@router.get("/admin/openai-models", dependencies=[Depends(require_owner)])
async def discover_openai_models():
    import httpx
    from pydantic_ai.profiles.openai import openai_model_profile

    from .config import model_config

    config = model_config("openai")
    if not config["key"]:
        return {"models": [], "message": "请先配置共用的 OpenAI 服务端凭据。"}
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(
                config["base_url"].rstrip("/") + "/models",
                headers={"Authorization": "Bearer " + config["key"]},
            )
            response.raise_for_status()
            names = sorted(
                {
                    item["id"]
                    for item in response.json().get("data", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and item["id"].startswith(("gpt-", "o1", "o3", "o4"))
                    and not any(
                        excluded in item["id"]
                        for excluded in [
                            "audio",
                            "realtime",
                            "image",
                            "transcribe",
                            "search",
                            "tts",
                            "instruct",
                        ]
                    )
                    and openai_model_profile(item["id"]).get(
                        "supports_json_schema_output"
                    )
                }
            )
        return {
            "models": names,
            "message": "已读取此凭据的模型列表；生成能力仍以实际请求为准。",
        }
    except Exception:
        return {
            "models": [],
            "message": "暂时未能读取 OpenAI 模型列表，请检查服务端凭据与连接。",
        }


@router.post("/admin/model-check", dependencies=[Depends(require_owner)])
async def model_check(
    body: ModelCheck | None = None,
    provider: Literal["deepseek", "openai"] | None = None,
    aid: UUID | None = None,
):
    import httpx

    from .config import model_config, resolve_model

    config = model_config(provider)
    if body and body.models:
        references = body.models
    else:
        from .admin_settings import effective

        with tx() as c:
            if aid:
                configs.check_scope(c, "models/" + str(aid))
            settings, _ = effective(c, "models", str(aid) if aid else None)
        references = [settings["default_model"], *settings["task_models"].values()]
    selected = sorted(
        {
            connection["model"]
            for ref in references
            if ref
            for connection in [resolve_model(ref)]
            if connection["provider"] == config["provider"]
        }
    ) or [config["model"]]
    if not config["key"]:
        return {
            "ok": False,
            "provider": config["provider"],
            "message": ("OpenAI" if config["provider"] == "openai" else "DeepSeek")
            + " 尚未配置服务端凭据。",
        }
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as web:
            response = await web.get(
                config["base_url"].rstrip("/") + "/models",
                headers={"Authorization": "Bearer " + config["key"]},
            )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data.get("data"), list):
            return {
                "ok": False,
                "provider": config["provider"],
                "message": "模型服务没有返回有效模型列表，尚未验证所选模型。",
            }
        available = {row.get("id") for row in data["data"] if isinstance(row, dict)}
        missing = [name for name in selected if name not in available]
        return {
            "ok": not missing,
            "provider": config["provider"],
            "checked_models": selected,
            "missing_models": missing,
            "message": "所选模型未出现在此凭据的可用列表中：" + "、".join(missing)
            if missing
            else "已在服务端模型列表中核对所选模型，尚未进行生成请求。",
        }
    except Exception:
        return {
            "ok": False,
            "message": "模型服务连接检查未通过，请核对服务端地址、凭据和连接。",
        }


@router.get("/admin/runtime-status")
def runtime_status(aid: UUID | None = None):
    from .admin_operations import work_window
    from .admin_settings import effective

    with tx() as c:
        if aid:
            configs.check_scope(c, "runtime/" + str(aid))
        policy, version = effective(c, "runtime", str(aid) if aid else None)
        states = all_rows(
            c,
            """SELECT r.agent_id,a.name,r.category,r.failures,r.last_error,r.next_retry_at,r.updated_at
            FROM lead.admin_runtime_state r JOIN lead.agents a ON a.id=r.agent_id
            WHERE a.desired_state<>'archived' AND (%s::uuid IS NULL OR r.agent_id=%s) ORDER BY r.updated_at DESC LIMIT 100""",
            (aid, aid),
        )
        leases = all_rows(
            c,
            "SELECT agent_id,category,expires_at FROM lead.admin_runtime_leases WHERE released_at IS NULL AND expires_at>now() AND (%s::uuid IS NULL OR agent_id=%s)",
            (aid, aid),
        )
    return {
        "policy_version": version,
        "window": work_window(policy["work_hours"]),
        "failures": states,
        "active_work": leases,
    }


@router.get("/admin/notification-recipients", dependencies=[Depends(require_owner)])
def notification_recipients():
    from .auth import auth_mode

    with tx() as c:
        rows = all_rows(
            c,
            "SELECT oidc_subject AS subject,role FROM lead.memberships WHERE status='active' ORDER BY oidc_subject",
        )
    if auth_mode() != "oidc":
        rows = [{"subject": "local", "role": "owner"}]
    return rows


@router.get("/admin/notifications")
def notification_list(before: datetime | None = None, before_id: UUID | None = None):
    from .admin_notifications import collect, visible

    collect()
    with tx() as c:
        return visible(c, before=before, before_id=before_id)


@router.post("/admin/notifications/{notification_id}/read")
def notification_read(notification_id: UUID):
    from .admin_notifications import mark_read

    with tx() as c:
        return mark_read(c, str(notification_id))
