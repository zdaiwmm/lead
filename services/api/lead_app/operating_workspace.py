"""Account-free channel preparation, weekly observations and readiness reports."""

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from .channel_calendar import default_schedule, future_slots
from .creative_pipeline import produce
from .creative_schema import COVER_SIZES, CreativeBrief, CreativeCard
from .db import all_rows, j, one, record, require_agent, tx, uid
from .operating_playbooks import (
    business_examples,
    catalog,
    channel_for,
    planning_context,
)
from .scope import current_tenant

router = APIRouter()


def channel_config(c, aid, platform, target_id=None):
    from .admin_config import strategy_for

    channel, version = strategy_for(c, platform, aid, target_id)
    return channel or channel_for(platform), version or catalog()["version"]


def zone_for(agent, business):
    from .channel_calendar import ChannelSchedule

    zone = (
        agent["onboarding_state"].get("operating_timezone")
        or business.get("facts", {}).get("timezone")
        or "UTC"
    )
    return ChannelSchedule(timezone=zone).timezone


def draft_pack(agent, business, channel, version):
    """Produce truthful copy/asset briefs before any social account exists."""
    name = business.get("facts", {}).get("name") or agent["name"]
    site = business.get("product_url") or ""
    examples = business_examples()
    use_examples = urlsplit(site).hostname == examples["hostname"]
    plans = []
    for plan in channel["plans"]:
        example = examples["plans"].get(plan["id"], {}) if use_examples else {}
        plans.append(
            {
                "plan_id": plan["id"],
                "name": plan["name"],
                "topics": example.get("first_batch", plan.get("first_batch", [])),
                "copy_or_script": example.get("sample", plan.get("template", "")),
                "cta": example.get("cta", plan.get("cta", "查看当前产品选项")),
                "destination_url": urljoin(site, example.get("destination_path", ""))
                if site
                else None,
                "formats": plan["formats"],
                "status": "source_based_draft",
            }
        )
    description = business["description"].strip()
    schedule = channel.get("schedule") or default_schedule(
        channel["platform"], zone_for(agent, business)
    )
    return {
        "platform": channel["platform"],
        "catalog_version": version,
        "business_profile_id": str(business["id"]),
        "config_revision": agent["config_revision"],
        "profile": {
            "display_name": name[:30],
            "bio": description[:70] + ("…" if len(description) > 70 else ""),
            "website": site or None,
            "welcome": name[:30]
            + " · "
            + description[:110]
            + ("…" if len(description) > 110 else ""),
            "disclosure": "品牌官方内容，由 AI 运营助手维护。",
            "status": "prepared_for_verified_account_setup",
        },
        "setup_steps": channel["setup"],
        "constraints": channel["constraints"],
        "schedule": schedule,
        "next_slots": [s.isoformat() for s in future_slots(schedule)],
        "plans": plans,
        "measurement": {
            "site_events": "requires_site_connection",
            "server_conversions": "requires_server_connection",
            "shared_profile_links": "channel_only",
        },
        "capabilities": {
            "local_media_generation": True,
            "captioned_video": True,
            "calendar_enforced": True,
            "social_login": "awaiting_account",
            "live_format_acceptance": "awaiting_account",
        },
    }


def prepare_workspace(aid, platform):
    if not channel_for(platform):
        raise ValueError("请选择 Telegram 以外的运营渠道")
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        if agent["desired_state"] == "paused":
            raise ValueError("Agent 已暂停，停止筹备新内容")
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if not business or not business["description"].strip():
            raise ValueError("请先保存业务介绍")
        channel, version = channel_config(c, aid, platform)
        pack = draft_pack(agent, business, channel, version)
        fingerprint = hashlib.sha256(
            json.dumps(pack, sort_keys=True, default=str).encode()
        ).hexdigest()
        # Calendar preview changes with time; identity is the versioned source, not that preview.
        key = f"channel-prepare:{aid}:{business['id']}:{agent['config_revision']}:{platform}:{version}"
        old = one(c, "SELECT id,result FROM lead.tasks WHERE workflow_id=%s", (key,))
        if old:
            return {"id": str(old["id"]), **old["result"], "duplicate": True}
        job_id = uid()
        c.execute(
            "INSERT INTO lead.tasks(id,tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,started_at,completed_at) VALUES(%s,%s,%s,'channel.prepare','succeeded',%s,%s,%s,now(),now(),now())",
            (
                job_id,
                current_tenant(),
                aid,
                key,
                j({"platform": platform, "source_fingerprint": fingerprint}),
                j(pack),
            ),
        )
        record(
            c,
            aid,
            "已备齐渠道运营底稿",
            channel["name"] + " · " + str(len(pack["plans"])) + " 套方案和账号资料草稿",
            task_id=job_id,
        )
        return {"id": job_id, **pack, "duplicate": False}


def prepare_all(aid):
    return [
        prepare_workspace(aid, channel["platform"]) for channel in catalog()["channels"]
    ]


async def prepare_example(aid, platform, plan_id=None, kind=None):
    """Render a real preview from an existing business; no account is connected."""
    pack = prepare_workspace(aid, platform)
    plan = (
        next((p for p in pack["plans"] if p["plan_id"] == plan_id), None)
        if plan_id
        else pack["plans"][0]
    )
    if not plan:
        raise ValueError("运营方案不存在")
    formats = plan["formats"]
    chosen = kind or ("image" if "image" in formats else "video")
    if chosen not in formats or chosen == "text":
        raise ValueError("此方案没有所选媒体格式")
    # An actual explanatory asset, explicitly not an unperformed screen demo.
    description = pack["profile"]["bio"]
    topic = (plan["topics"] or [plan["name"]])[0]
    brief = CreativeBrief(
        kind=chosen,
        headline=topic[:100],
        cards=[
            CreativeCard(
                heading=pack["profile"]["display_name"],
                body=description[:70] + ("…" if len(description) > 70 else ""),
            ),
            CreativeCard(
                heading="开始前 / Before you begin",
                body="明确任务、准备有权使用的材料，并设定结果检查标准。",
            ),
            CreativeCard(
                heading="下一步 / Next step",
                body="进入产品页面核对当前选项与适用条件，再决定如何开始。",
            ),
        ],
        cta=plan["cta"][:140],
    )
    return await produce(
        aid,
        platform,
        brief,
        business_id=pack["business_profile_id"],
        config_revision=pack["config_revision"],
        purpose="account_free_preview",
    )


async def prepare_profile_assets(aid, platform):
    pack = prepare_workspace(aid, platform)
    brief = CreativeBrief(
        headline=pack["profile"]["display_name"],
        cards=[CreativeCard(heading="Brand", body=pack["profile"]["bio"])],
        cta="Explore the current options",
    )
    results = {}
    for purpose in (
        ["profile_avatar", "profile_cover"]
        if platform in COVER_SIZES
        else ["profile_avatar"]
    ):
        results[purpose] = await produce(
            aid,
            platform,
            brief,
            business_id=pack["business_profile_id"],
            config_revision=pack["config_revision"],
            purpose=purpose,
        )
    return results


def weekly_review(c, aid, contexts, now=None):
    now = now or datetime.now(timezone.utc)
    week = now.strftime("%G-W%V")
    for target_id, context in contexts.items():
        key = ":".join(
            (
                week,
                target_id,
                context["catalog_version"],
                context["objective"],
                context.get("business_profile_id", ""),
                str(context.get("config_revision", "")),
            )
        )
        if one(
            c,
            "SELECT 1 FROM lead.memory_entries WHERE kind='observation' AND body->>'review_key'=%s AND deleted_at IS NULL",
            (key,),
        ):
            continue
        preferred = context.get("preferred_plan_id")
        body = {
            "type": "channel_weekly_review",
            "review_key": key,
            "target_id": target_id,
            "platform": context["platform"],
            "catalog_version": context["catalog_version"],
            "objective": context["objective"],
            "coverage": context.get("coverage"),
            "state": context["state"],
            "stats": context.get("stats", []),
            "preferred_plan_id": preferred,
            "next_action": "继续探索并检查转化回传。"
            if not preferred
            else "优先采用已有观察支持的方案，每五次保留一次挑战方案。",
            "limits": "这是一周的运营观察，不能推定因果、收入或缺失的站内行为。",
        }
        c.execute(
            "INSERT INTO lead.memory_entries(tenant_id,agent_id,kind,body,source_refs,confirmation) VALUES(%s,%s,'observation',%s,%s,'system_observed')",
            (
                current_tenant(),
                aid,
                j(body),
                j(
                    [
                        {
                            "target_id": target_id,
                            "catalog_version": context["catalog_version"],
                        }
                    ]
                ),
            ),
        )
        record(
            c,
            aid,
            "已完成渠道周复盘",
            context["platform"] + " · " + body["next_action"],
        )


class PreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str | None = Field(default=None, max_length=20)
    produce_media: bool = False
    profile_assets: bool = False
    plan_id: str | None = Field(default=None, max_length=120)
    kind: str | None = Field(default=None, pattern=r"^(image|video)$")


@router.post("/api/agents/{aid}/operations/prepare")
async def prepare(aid: UUID, body: PreparationRequest):
    aid = str(aid)
    if body.profile_assets:
        if not body.platform:
            raise ValueError("制作头像和封面时需要指定渠道")
        return await prepare_profile_assets(aid, body.platform)
    if body.produce_media:
        if not body.platform:
            raise ValueError("制作预览时需要指定渠道")
        return await prepare_example(aid, body.platform, body.plan_id, body.kind)
    return prepare_workspace(aid, body.platform) if body.platform else prepare_all(aid)


@router.get("/api/agents/{aid}/operations")
def status(aid: UUID):
    aid = str(aid)
    with tx(aid) as c:
        agent = require_agent(c, aid)
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        packs = all_rows(
            c,
            "SELECT id,result,created_at FROM lead.tasks WHERE type='channel.prepare' AND result->>'business_profile_id'=%s AND result->>'config_revision'=%s ORDER BY created_at DESC",
            (str(business["id"]) if business else "", str(agent["config_revision"])),
        )
        targets = all_rows(
            c,
            "SELECT t.id AS target_id,s.platform FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.state='active' AND s.state='connected'",
        )
        feedback = (
            planning_context(c, targets, business, agent["config_revision"])
            if business
            else {}
        )
        reviews = all_rows(
            c,
            "SELECT body,created_at FROM lead.memory_entries WHERE kind='observation' AND body->>'type'='channel_weekly_review' AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 20",
        )
        jobs = all_rows(
            c,
            "SELECT id,state,input->>'platform' AS platform,input->'brief'->>'headline' AS headline,input->>'purpose' AS purpose,result->'assets' AS assets,result->>'kind' AS kind,result->>'error' AS error,completed_at FROM lead.tasks WHERE type='media.generate' ORDER BY created_at DESC LIMIT 30",
        )
    by_platform = {p["result"]["platform"]: p for p in reversed(packs)}
    return {
        "channels": [
            {
                "platform": p["platform"],
                "preparation": by_platform.get(p["platform"]),
                "account_required_for_live_verification": True,
            }
            for p in catalog()["channels"]
        ],
        "feedback": feedback,
        "reviews": reviews,
        "media_jobs": jobs,
    }
