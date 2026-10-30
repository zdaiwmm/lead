"""Community discovery and participation policy; discovery never grants access.

No messages, member lists, or chat histories are collected in this workflow.
External admin/rules pages are evidence for candidates, not consent from members.
"""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .db import all_rows, audit, emit, j, one, record, require_agent, tx
from .scope import current_tenant


class GrowthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    mode: Literal["discovery", "contextual_outreach"] = "discovery"
    history_message_limit: int = Field(default=30, ge=5, le=100)
    topics: list[str] = Field(
        default_factory=lambda: ["外语阅读", "长文与 EPUB 翻译", "协作翻译"],
        min_length=1,
        max_length=8,
    )
    discovery_interval_hours: int = Field(default=24, ge=6, le=168)
    max_candidates_per_run: int = Field(default=5, ge=1, le=10)
    max_joins_per_day: int = Field(default=1, ge=0, le=100)
    max_replies_per_day: int = Field(default=2, ge=0, le=100)
    max_promotions_per_week: int = Field(default=1, ge=0, le=3)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=120)
    telegram_url: str = Field(max_length=180)
    source_url: str = Field(max_length=2000)
    reading_need: str = Field(max_length=400)
    rules_summary: str = Field(max_length=600)


class Discovery(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list, max_length=10)
    summary: str = Field(max_length=800)


def get_strategy(aid):
    with tx(aid) as c:
        a = require_agent(c, aid)
        config = GrowthConfig.model_validate(
            a["onboarding_state"].get("community_growth", {})
        )
        runs = all_rows(
            c,
            "SELECT id,state,result,created_at FROM lead.tasks WHERE type IN ('community.discover','community.engage') ORDER BY created_at DESC LIMIT 20",
        )
        return {
            "config": config.model_dump(),
            "schedule": a["onboarding_state"].get("community_schedule"),
            "execution_scope": "contextual_outreach"
            if config.mode == "contextual_outreach"
            else "public_admin_page_discovery",
            "runs": runs,
            "participation": {
                "join": "requires_owner_delegation_and_group_rules",
                "read_or_reply": "bounded_accessible_history_and_contextual_questions",
                "direct_messages": False,
                "disclose_brand_and_AI": False,
                "disclose_brand_when_promoting": True,
                "proactive_AI_introduction": False,
                "truthful_identity_answers": True,
            },
        }


def set_strategy(aid, config):
    config = GrowthConfig.model_validate(config)
    with tx(aid) as c:
        require_agent(c, aid, True)
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s,version=version+1,updated_at=now() WHERE id=%s",
            (j({"community_growth": config.model_dump()}), aid),
        )
        if config.enabled and config.mode == "contextual_outreach":
            for account in all_rows(
                c,
                "SELECT id,external_account_id FROM lead.social_accounts WHERE platform='telegram' AND state='connected' AND automation_enabled",
            ):
                existing = one(
                    c,
                    "SELECT id FROM lead.interaction_cards WHERE type='manage_community' AND state='accepted' AND expires_at>now() AND payload->>'account_id'=%s AND payload->>'external_account_id'=%s",
                    (str(account["id"]), account["external_account_id"]),
                )
                if not existing:
                    c.execute(
                        "INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,state,payload,expires_at) VALUES(%s,%s,'manage_community','accepted',%s,now()+interval '1 year')",
                        (
                            current_tenant(),
                            aid,
                            j(
                                {
                                    "account_id": str(account["id"]),
                                    "external_account_id": account[
                                        "external_account_id"
                                    ],
                                    "source": "owner_strategy",
                                    "allowed_actions": [
                                        "join_public_group",
                                        "read_recent_history",
                                        "contextual_reply",
                                        "relevant_promotion",
                                    ],
                                    "account_risk_accepted": True,
                                }
                            ),
                        ),
                    )
        audit(c, aid, "community.strategy_updated", config.model_dump())
        record(
            c,
            aid,
            "已更新社群培育策略",
            "寻找阅读与翻译需求；先核对规则和参与许可，再考虑互动。",
        )
        emit(c, aid, "supervisor.wake")
    return get_strategy(aid)


def request_research(aid, request_id):
    from uuid import UUID

    request_id = str(UUID(str(request_id)))
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        if not GrowthConfig.model_validate(
            a["onboarding_state"].get("community_growth", {})
        ).enabled:
            raise ValueError("社群策略尚未启用")
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
            (j({"community_research_request": request_id}), aid),
        )
        audit(c, aid, "community.research_requested", {"request_id": request_id})
        emit(c, aid, "supervisor.wake")
    return {"ok": True, "request_id": request_id}


def participation_gate(
    candidate, action, *, relevant_question=False, promotion_requested=False
):
    """Deterministic gate; model relevance judgments cannot create permissions."""
    if action not in {"join", "reply", "promote"}:
        return {"allowed": False, "reason": "unsupported_action"}
    if candidate.get("reports") or candidate.get("permission_revoked"):
        return {"allowed": False, "reason": "participation_stopped"}
    if not candidate.get("rules_evidence") or not candidate.get(
        "admin_automation_permission"
    ):
        return {"allowed": False, "reason": "community_permission_required"}
    if candidate.get("kind") == "broadcast" and action != "join":
        return {"allowed": False, "reason": "discussion_destination_required"}
    if action in {"reply", "promote"}:
        if not candidate.get("history_access_verified"):
            return {"allowed": False, "reason": "history_access_required"}
        if not relevant_question:
            return {"allowed": False, "reason": "no_relevant_question"}
    if action == "promote" and (
        not promotion_requested or not candidate.get("commercial_permission")
    ):
        return {"allowed": False, "reason": "promotion_not_invited"}
    return {"allowed": True, "reason": "within_owner_strategy"}


def verified_candidates(c, output, sources):
    """Accept only links actually observed on non-Telegram external pages."""
    found = []
    seen = set()
    for candidate in output.candidates:
        parsed = urlsplit(candidate.telegram_url)
        handle = parsed.path.strip("/")
        if (
            parsed.scheme != "https"
            or parsed.hostname != "t.me"
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", handle)
        ):
            continue
        source = None
        observed = None
        from .community_links import telegram_link

        for evidence in reversed(sources):
            if evidence["url"] != candidate.source_url or not evidence.get(
                "evidence_id"
            ):
                continue
            if (urlsplit(candidate.source_url).hostname or "").endswith("telegram.org"):
                if not evidence.get("native") or not one(
                    c,
                    "SELECT 1 FROM lead.browser_runs WHERE id=%s AND mode='community_native_discovery'",
                    (evidence["run_id"],),
                ):
                    continue
            step = one(
                c,
                "SELECT output FROM lead.browser_steps WHERE id=%s AND run_id=%s AND state='completed'",
                (evidence["evidence_id"], evidence["run_id"]),
            )
            if not step:
                continue
            links = {
                normalized.casefold()
                for entry in step["output"].get("elements", [])
                + step["output"].get("community_links", [])
                if (normalized := telegram_link(entry.get("href")))
            }
            if candidate.telegram_url.casefold() in links:
                source, observed = evidence, step["output"]
                break
        if not source or candidate.telegram_url.casefold() in seen:
            continue
        seen.add(candidate.telegram_url.casefold())
        native_name = next(
            (
                group["name"]
                for group in observed.get("groups", [])
                if group["telegram_url"].casefold() == candidate.telegram_url.casefold()
            ),
            None,
        )
        found.append(
            {
                **candidate.model_dump(),
                "name": native_name or candidate.name,
                "state": "discovered",
                "evidence_id": source["evidence_id"],
                "capability_verification": {
                    "join": "unverified",
                    "read_messages": "unverified",
                    "reply": "unverified",
                    "promote": "unverified",
                },
            }
        )
    return found


async def discover_communities(aid):
    from .admin_controls import channel_policy
    with tx(aid) as c:
        policy, _ = channel_policy(c, aid, "telegram")
        if not policy["enabled"] or not policy["allow_community_growth"]:
            return None
    from .admin_settings import tool_enabled

    if not tool_enabled(aid, "community_research") or not tool_enabled(
        aid, "public_web"
    ):
        return None
    from .community_discovery import advance

    return await advance(aid)
