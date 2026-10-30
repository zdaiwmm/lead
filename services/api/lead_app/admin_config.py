"""Versioned workspace configuration shared by the admin UI and Agent runtime."""

import copy
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .channel_calendar import ChannelSchedule
from .config import env
from .db import all_rows, audit, j, one, require_agent
from .scope import principal

MAX_TOKENS = 9_007_199_254_740_991
FIELD_DEFAULTS = {
    "product_name": ("产品名称", "例如 Seka", True, 80),
    "product_url": ("产品网址", "https://", False, 2000),
    "description": ("产品介绍", "你的产品解决什么问题，有哪些核心卖点？", True, 10000),
    "audience": ("目标客户", "希望吸引哪些人？他们面临什么问题？", False, 2000),
    "goal": ("运营目标", "例如获取产品试用、客户咨询", True, 2000),
    "content_expectations": (
        "运营内容期望",
        "内容主题、语言、语气，以及希望避免的内容",
        False,
        2000,
    ),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BudgetConfig(StrictModel):
    daily_token_limit: int | None = Field(
        default=None, strict=True, ge=0, le=MAX_TOKENS
    )


class SetupField(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    placeholder: str = Field(default="", max_length=400)
    help: str = Field(default="", max_length=400)
    required: bool = False
    default: str = Field(default="", max_length=10000)


class StrategyOption(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=300)


class OnboardingConfig(StrictModel):
    steps: list[str] = Field(min_length=3, max_length=3)
    fields: dict[str, SetupField]
    strategies: list[StrategyOption] = Field(min_length=1, max_length=8)
    default_platform: str = "telegram"


class StrategyPlan(StrictModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=100)
    audience: str = Field(default="", max_length=2000)
    hypothesis: str = Field(default="", max_length=4000)
    formats: list[str] = Field(min_length=1, max_length=3)
    assets: str = Field(default="", max_length=4000)
    workflow: list[str] = Field(default_factory=list, max_length=30)
    first_batch: list[str] = Field(default_factory=list, max_length=30)
    template: str = Field(default="", max_length=10000)
    cta: str = Field(default="", max_length=1000)
    destination_path: str = Field(default="", max_length=1000)
    experiment: str = Field(default="", max_length=4000)
    iterate: str = Field(default="", max_length=4000)


class ChannelStrategy(StrictModel):
    platform: str
    name: str = Field(min_length=1, max_length=100)
    target: str = Field(default="自有账号", max_length=500)
    maturity_days: int = Field(default=7, strict=True, ge=1, le=90)
    cadence: str = Field(min_length=1, max_length=2000)
    schedule: ChannelSchedule | None = None
    setup: list[str] = Field(default_factory=list, max_length=30)
    conversion_path: str = Field(default="", max_length=4000)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    sources: list[str] = Field(default_factory=list, max_length=30)
    plans: list[StrategyPlan] = Field(min_length=1, max_length=12)
    experience_notes: str = Field(default="", max_length=10000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)


def onboarding_default():
    fields = {
        key: {
            "label": label,
            "placeholder": hint,
            "help": "",
            "required": required,
            "default": "",
        }
        for key, (label, hint, required, _) in FIELD_DEFAULTS.items()
    }
    fields["goal"]["default"] = "获得更多有效访问和潜在客户"
    return {
        "steps": ["产品信息", "运营方向", "连接渠道"],
        "fields": fields,
        "strategies": [
            {"name": "稳健持续", "description": "先验证内容方向，再逐步扩大运营。"},
            {"name": "快速验证", "description": "小步尝试多个选题，根据反馈及时调整。"},
            {"name": "内容价值优先", "description": "优先提供实用内容，减少直接促销。"},
        ],
        "default_platform": "telegram",
    }


def parse_key(key):
    from .domain import PLATFORMS

    parts = key.split("/")
    aid = target = platform = None
    if parts[0] == "budgets" and len(parts) == 2:
        kind = "budget"
        if parts[1] != "default":
            aid = str(UUID(parts[1]))
    elif key == "onboarding/default":
        kind = "onboarding"
    elif key == "model-catalog/default":
        kind = "model_catalog"
    elif key in {
        "member-access/default",
        "pricing/default",
        "external-notifications/default",
    }:
        kind = {
            "member-access/default": "member_access",
            "pricing/default": "pricing",
            "external-notifications/default": "external_notifications",
        }[key]
    elif (
        parts[0] in {"models", "brand", "runtime", "notifications", "attribution"}
        and len(parts) == 2
    ):
        kind = parts[0]
        if parts[1] != "default":
            aid = str(UUID(parts[1]))
    elif parts[0] in {"strategies", "channel-rules"} and 2 <= len(parts) <= 4:
        kind, platform = (
            ("strategy" if parts[0] == "strategies" else "channel_rule"),
            parts[1],
        )
        if platform not in {p[0] for p in PLATFORMS}:
            raise ValueError("请选择有效的渠道")
        if len(parts) > 2:
            aid = str(UUID(parts[2]))
        if len(parts) > 3:
            target = str(UUID(parts[3]))
    elif parts[0] == "experiences" and len(parts) == 3:
        kind, aid = "experience", str(UUID(parts[1]))
        UUID(parts[2])
    else:
        raise ValueError("配置项不存在")
    return kind, aid, platform, target


def check_scope(c, key, lock=False):
    kind, aid, platform, target = parse_key(key)
    if aid:
        require_agent(c, aid, lock)
    if target and not one(
        c,
        "SELECT t.id FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s AND t.agent_id=%s AND s.platform=%s",
        (target, aid, platform),
    ):
        raise LookupError("渠道账号目标不存在")
    return kind, aid, platform, target


def head(c, key):
    return one(c, "SELECT * FROM lead.admin_configs WHERE config_key=%s", (key,))


def published(c, key):
    row = head(c, key)
    return row["body"] if row and row["body"] is not None else None


def default_body(c, key):
    kind, aid, platform, target = parse_key(key)
    if kind in {
        "experience",
        "model_catalog",
        "member_access",
        "pricing",
        "external_notifications",
    }:
        from .admin_settings import defaults

        return defaults(kind)
    if kind == "channel_rule":
        from .admin_settings import defaults

        if target:
            return read(c, f"channel-rules/{platform}/{aid}")["body"]
        return read(c, f"channel-rules/{platform}")["body"] if aid else defaults(kind)
    if kind in {"models", "brand", "runtime", "notifications", "attribution"}:
        from .admin_settings import defaults

        return read(c, f"{kind}/default")["body"] if aid else defaults(kind)
    if kind == "budget":
        if aid:
            return {
                "daily_token_limit": require_agent(c, aid)["onboarding_state"].get(
                    "daily_token_limit"
                )
            }
        return {
            "daily_token_limit": int(
                env().get("LEAD_DAILY_TOKEN_LIMIT") or 1_000_000_000
            )
        }
    if kind == "onboarding":
        return onboarding_default()
    if target:
        return read(c, f"strategies/{platform}/{aid}")["body"]
    if aid:
        return read(c, f"strategies/{platform}")["body"]
    from .operating_playbooks import channel_for

    channel = channel_for(platform)
    if channel:
        return ChannelStrategy.model_validate(channel).model_dump()
    return ChannelStrategy(
        platform=platform,
        name="Telegram",
        target="自有广播频道",
        cadence="每天最多 3 条，以实际授权上限为准。",
        setup=["核对业务资料、账号身份及目标频道"],
        plans=[
            StrategyPlan(
                id="telegram.useful_note",
                name="实用内容与产品场景",
                formats=["text"],
                hypothesis="用可核对的实用内容帮助受众理解产品",
                template="问题、方法、适用条件、产品入口",
            )
        ],
    ).model_dump()


def read(c, key):
    kind, aid, _, _ = check_scope(c, key)
    if kind == "member_access" and principal() and principal()["role"] != "owner":
        from fastapi import HTTPException

        raise HTTPException(403, "只有所有者可以查看成员权限配置")
    row = head(c, key)
    return {
        "key": key,
        "kind": kind,
        "agent_id": aid,
        "version": row["version"] if row else 0,
        "published_version": row["published_version"] if row else 0,
        "body": row["body"]
        if row and row["body"] is not None
        else default_body(c, key),
        "draft": row["draft"] if row else None,
        "inherited": not row or row["body"] is None,
    }


def validate(key, body, c=None):
    kind, aid, platform, _ = parse_key(key)
    from .admin_settings import SCHEMAS

    try:
        data = (
            {
                "budget": BudgetConfig,
                "onboarding": OnboardingConfig,
                "strategy": ChannelStrategy,
                **SCHEMAS,
            }[kind]
            .model_validate(body)
            .model_dump(mode="json")
        )
    except ValidationError as exc:
        issue = exc.errors()[0]
        raise ValueError(
            "配置格式有误：" + ".".join(map(str, issue["loc"])) + "，" + issue["msg"]
        ) from None
    if kind in SCHEMAS:
        from .admin_settings import validate as validate_settings

        validate_settings(kind, data, c=c)
        return data
    if kind == "budget" and not aid and data["daily_token_limit"] is None:
        raise ValueError("默认额度不能为空")
    if kind == "onboarding":
        from .domain import PLATFORMS

        if set(data["fields"]) != set(FIELD_DEFAULTS):
            raise ValueError("创建引导必须保留全部业务字段")
        if data["default_platform"] not in {p[0] for p in PLATFORMS}:
            raise ValueError("默认渠道不存在")
        if any(not s or len(s) > 30 for s in data["steps"]):
            raise ValueError("步骤名称需要 1–30 个字符")
        for key, field in data["fields"].items():
            if FIELD_DEFAULTS[key][2] and not field["required"]:
                raise ValueError(field["label"] + "是创建 Agent 的必要信息")
            if len(field["default"]) > FIELD_DEFAULTS[key][3]:
                raise ValueError(field["label"] + "的默认值过长")
        names = [s["name"] for s in data["strategies"]]
        if len(names) != len(set(names)):
            raise ValueError("策略选项名称不能重复")
        url = data["fields"]["product_url"]["default"]
        if url and not re.match(r"^https?://[^/\s]+", url):
            raise ValueError("默认网址需要使用完整的 http 或 https 地址")
    if kind == "strategy":
        if data["platform"] != platform:
            raise ValueError("策略渠道与所选渠道不一致")
        ids = [p["id"] for p in data["plans"]]
        if len(ids) != len(set(ids)):
            raise ValueError("策略方案标识不能重复")
        for plan in data["plans"]:
            if not set(plan["formats"]) <= {"text", "image", "video"}:
                raise ValueError("内容格式只支持文字、图片、视频")
            if plan["destination_path"] and not re.fullmatch(
                r"/(?!/)[A-Za-z0-9/_-]*", plan["destination_path"]
            ):
                raise ValueError("承接路径需要是产品网站内的路径，如 /translate")
        lists = [
            data["setup"],
            data["constraints"],
            data["sources"],
            data["evidence_refs"],
        ]
        for plan in data["plans"]:
            lists.extend([plan["workflow"], plan["first_batch"]])
        if any(not s.strip() or len(s) > 2000 for values in lists for s in values):
            raise ValueError("列表条目不能为空或超过 2000 个字符")
    return data


def write(
    c,
    key,
    body,
    expected_version,
    action="publish",
    note="",
    rollback_version=None,
    reset_to_default=False,
):
    """Compare-and-swap, runtime effects and immutable history share one commit."""
    who = principal()
    from .admin_permissions import require_config

    require_config(key)
    kind, aid, platform, target = check_scope(c, key, lock=True)
    if kind in {"models", "model_catalog"}:
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(lead.ctx_tenant()::text || ':model-catalog',0))"
        )
    c.execute(
        "INSERT INTO lead.admin_configs(tenant_id,agent_id,config_key,kind) VALUES(lead.ctx_tenant(),%s,%s,%s) ON CONFLICT DO NOTHING",
        (aid, key, kind),
    )
    row = one(
        c, "SELECT * FROM lead.admin_configs WHERE config_key=%s FOR UPDATE", (key,)
    )
    if row["version"] != expected_version:
        raise ValueError("配置已被其他人更新，请刷新后核对；你的修改尚未保存")
    before = {
        "body": row["body"] if row["body"] is not None else default_body(c, key),
        "draft": row["draft"],
        "inherited": row["body"] is None,
    }
    if action == "rollback":
        old = one(
            c,
            "SELECT after_state FROM lead.admin_config_versions WHERE config_id=%s AND version=%s AND action IN ('publish','rollback')",
            (row["id"], rollback_version),
        )
        if not old:
            raise LookupError("找不到可回滚的已发布版本")
        body = old["after_state"]["body"]
        reset_to_default = old["after_state"].get("inherited", False)
    if reset_to_default:
        if (
            kind
            not in {
                "strategy",
                "models",
                "brand",
                "runtime",
                "notifications",
                "channel_rule",
                "attribution",
            }
            or action == "draft"
        ):
            raise ValueError("此配置不支持恢复继承")
        body = default_body(c, key)
    data = validate(key, body, c=c)
    if kind == "experience":
        from .admin_controls import validate_experience

        data = validate_experience(c, aid, data)
    if kind == "member_access":
        from .admin_permissions import validate_members

        validate_members(c, data)
    if kind == "external_notifications":
        from .admin_delivery import validate as validate_delivery

        validate_delivery(c, data)
    if kind == "model_catalog" and action != "draft":
        from .config import model_config

        allowed = (
            set(data["openai_models"])
            | {model_config("openai")["model"]}
            | {
                s.strip()
                for s in (env().get("OPENAI_ALLOWED_MODELS") or "").split(",")
                if s.strip()
            }
            | {
                s.strip().split(":", 1)[1]
                for s in (env().get("LEAD_ALLOWED_MODELS") or "").split(",")
                if s.strip().startswith("openai:")
            }
        )
        used = all_rows(
            c,
            "SELECT body FROM lead.admin_configs WHERE kind='models' AND body IS NOT NULL AND (agent_id IS NULL OR agent_id IN(SELECT id FROM lead.agents WHERE desired_state<>'archived'))",
        )
        for record in used:
            for ref in [
                record["body"]["default_model"],
                *record["body"]["task_models"].values(),
            ]:
                if ref.startswith("openai:") and ref.split(":", 1)[1] not in allowed:
                    raise ValueError(
                        "有运行配置正在使用模型 " + ref + "，请先调整后再移除候选"
                    )
    if kind == "notifications" and data["recipient_subjects"]:
        members = {
            m["oidc_subject"]
            for m in all_rows(
                c, "SELECT oidc_subject FROM lead.memberships WHERE status='active'"
            )
        }
        if not set(data["recipient_subjects"]) <= members | {"local"}:
            raise ValueError("通知接收人必须是当前工作室的有效成员")
    version = row["version"] + 1
    after = {
        "body": before["body"] if action == "draft" else data,
        "draft": data if action == "draft" else None,
        "inherited": before["inherited"] if action == "draft" else reset_to_default,
    }
    c.execute(
        "UPDATE lead.admin_configs SET version=%s,published_version=%s,body=%s,draft=%s,updated_at=now() WHERE id=%s",
        (
            version,
            row["published_version"] if action == "draft" else version,
            j(row["body"])
            if action == "draft" and row["body"] is not None
            else None
            if action == "draft" or reset_to_default
            else j(data),
            j(after["draft"]) if after["draft"] is not None else None,
            row["id"],
        ),
    )
    actor = (
        (who.get("issuer", "local") + ":" + who["subject"]) if who else "local:internal"
    )
    c.execute(
        "INSERT INTO lead.admin_config_versions(tenant_id,agent_id,config_id,config_key,version,action,before_state,after_state,actor_ref,note) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (aid, row["id"], key, version, action, j(before), j(after), actor, note),
    )
    if action != "draft" and kind == "budget" and aid:
        from .budget import apply_daily_limit

        apply_daily_limit(c, aid, data["daily_token_limit"])
    elif action != "draft" and kind == "budget":
        from .db import emit

        c.execute(
            """UPDATE lead.budget_periods b SET limit_units=%s FROM lead.agents a
            WHERE b.agent_id=a.id AND a.onboarding_state->>'daily_token_limit' IS NULL
            AND b.unit='tokens' AND b.starts_at<=now() AND b.ends_at>now()""",
            (data["daily_token_limit"],),
        )
        for job in all_rows(
            c,
            """UPDATE lead.chat_jobs j SET status='pending',error_code=NULL
            FROM lead.agents a WHERE j.agent_id=a.id AND a.onboarding_state->>'daily_token_limit' IS NULL
            AND j.status IN ('blocked','failed') AND j.error_code='BUDGET_EXCEEDED' RETURNING j.id,j.agent_id,j.message_id""",
        ):
            c.execute(
                "UPDATE lead.conversation_messages SET status='pending' WHERE id=%s",
                (job["message_id"],),
            )
            emit(c, str(job["agent_id"]), "chat.enqueue", {"job_id": str(job["id"])})
        for agent in all_rows(
            c,
            "SELECT id FROM lead.agents WHERE desired_state<>'archived' AND onboarding_state->>'daily_token_limit' IS NULL",
        ):
            emit(c, str(agent["id"]), "supervisor.wake")
    if action != "draft" and kind in {"models", "brand", "runtime", "attribution"}:
        from .admin_operations import configuration_published

        configuration_published(c, kind, aid)
    if action != "draft" and kind == "channel_rule":
        from .admin_controls import channel_rule_published

        channel_rule_published(c, platform, aid, target)
    audit(
        c, aid, "admin.config." + action, {"key": key, "version": version, "note": note}
    )
    return read(c, key)


def history(c, key=None, before=None, limit=40, before_id=None):
    if key:
        check_scope(c, key)
    return all_rows(
        c,
        """
        SELECT id,config_key,version,action,before_state,after_state,actor_ref,note,created_at
        FROM lead.admin_config_versions WHERE (%s::text IS NULL OR config_key=%s)
        AND (%s::timestamptz IS NULL OR (created_at,id)<(%s,%s::uuid))
        AND (%s OR config_key<>'member-access/default')
        ORDER BY created_at DESC,id DESC LIMIT %s
    """,
        (
            key,
            key,
            before,
            before,
            before_id or "00000000-0000-0000-0000-000000000000",
            not principal() or principal()["role"] == "owner",
            limit,
        ),
    )


def onboarding_snapshot(c, version=None):
    config = read(c, "onboarding/default")
    if version is None or version == config["published_version"]:
        return {"version": config["published_version"], "body": config["body"]}
    if version == 0:
        return {"version": 0, "body": onboarding_default()}
    row = one(
        c,
        "SELECT after_state FROM lead.admin_config_versions WHERE config_key='onboarding/default' AND version=%s AND action IN ('publish','rollback')",
        (version,),
    )
    if not row:
        raise ValueError("创建引导版本不存在，请重新打开创建流程")
    return {"version": version, "body": row["after_state"]["body"]}


def validate_setup(profile, snapshot):
    for key, field in snapshot["body"]["fields"].items():
        if field["required"] and not profile.get(key, "").strip():
            raise ValueError("请填写" + field["label"])


def strategy_for(c, platform, aid, target):
    """Specific overrides win; absence preserves the existing planner behavior."""
    for key in [
        f"strategies/{platform}/{aid}/{target}",
        f"strategies/{platform}/{aid}",
        f"strategies/{platform}",
    ]:
        row = head(c, key)
        if row and row["body"] is not None:
            return copy.deepcopy(row["body"]), f"{key}@{row['published_version']}"
    return None, None
