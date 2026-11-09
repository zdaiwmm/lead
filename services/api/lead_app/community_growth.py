"""Community discovery and participation policy; discovery never grants access.

No messages, member lists, or chat histories are collected in this workflow.
External admin/rules pages are evidence for candidates, not consent from members.
"""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import all_rows, audit, emit, j, one, record, require_agent, tx
from .scope import current_tenant

# This experiment is enabled explicitly per Agent, independently of its name.
CHAMBER_TOPICS = ("language exchange", "远程协作", "digital nomads", "兴趣交流", "product showcase", "旅行搭子", "privacy tools", "婚外情", "外遇", "婚外关系", "低调交往", "匿名情感倾诉", "extramarital relationships", "discreet dating")
CHAMBER_BRIEF = (
    "跨语言、场景和来源广泛探索实际可参与的社群。轮换交友（含婚外关系讨论）、兴趣、语言交换、临时协作、旅行、"
    "工具展示和其他有真人的公开讨论，无需预先证明隐私需求。核实讨论群身份，不以订阅数冒充可触达成员。"
    "根据相同帖龄的独立短链有效点击调整分发，同时继续探索新场景。"
)


def chamber_scenario_priority(public_topic):
    if re.search(r"婚外|外遇|不倫|不伦|低[调調]交往|地下[情戀恋]|extramarital|discreet dating|secret relationship|\brela(?:ții|tii) extraconjugale\b|\brelations? extra[- ]?conjugales?\b|\b(?:amor|parejas?|relaciones?|corazones rotos)\b.{0,65}\binfidelidad(?:es)?\b|\binfidelidad(?:es)? (?:en (?:la|las) )?parejas?\b|\baffair (?:partners?|dating|relationships?)\b", public_topic, re.I):
        return 2
    if re.search(r"情感[倾傾]訴|情感倾诉|匿名[树樹]洞|私密[关關][系係]|匿名情感|anonymous confessions?", public_topic, re.I):
        return 1
    return 0


def excluded_group_topic(topic):
    return bool(re.search(r"\b(?:minors?|teens?|schoolchildren|underage|escort|escorts)\b|未成年|中学生|中學生|援交|подростк|школьник|несовершеннолет|эскорт", topic, re.I))


def adjacent_topic(topic):
    if excluded_group_topic(topic):
        return False
    return bool(re.search(
        r"\b(?:dating|singles|flirting|matchmaking|romance|confessions?|relationship advice)\b|"
        r"language exchange|digital nomads?|remote work|coworking|product showcase|privacy tools|travel buddies|gaming|hobbies|语言交换|协作|旅行|兴趣|工具展示|ゲーム|intercambio de idiomas|échange linguistique|"
        r"find (?:new )?friends or love|交友|约会|約會|相亲|相親|戀愛|恋爱|感情相談|恋愛|"
        r"rencontres? amoureuses|знакомств|про любовь|семейные отношения|恋愛相談|既婚者交流|\bevlilik\b.{0,35}\b(?:arkadaşlık|tanışma|sohbet)\b|\b(?:arkadaşlık|tanışma)\b.{0,35}\bevlilik\b|citas para solteros",
        topic, re.I,
    ))


def audience_lane(topic):
    patterns = (("language",r"language|idiomas|语言|linguistique"),("travel",r"travel|nomad|旅行"),
                ("collaboration",r"remote|cowork|协作|freelanc"),("tools",r"tools?|privacy|showcase|工具"),
                ("interests",r"hobb|gaming|兴趣|ゲーム"),("social",r"dating|singles|solter[oa]s?|infidelidad|citas|pareja|encontros|rencontre|交友|恋爱|relationship|婚外"))
    return next((name for name,pattern in patterns if re.search(pattern,topic,re.I)), "other")


def observation_topic(topic, counts, *, widen=False):
    return adjacent_topic(topic) and (counts.get("members", 0) >= (30 if widen else 100) or counts.get("online", 0) >= 5)


class PlacementPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repeat_cooldown_hours: float = Field(default=2, ge=1, le=168)
    min_new_messages: int = Field(default=6, ge=1, le=100)
    min_distinct_messages: int = Field(default=2, ge=1, le=100)
    similarity_threshold: float = Field(default=0.78, ge=0.5, le=0.95)
    advertising_posts_per_24h: int = Field(default=2, ge=1, le=10)


class GrowthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    mode: Literal["discovery", "contextual_outreach"] = "discovery"
    history_message_limit: int = Field(default=30, ge=5, le=100)
    topics: list[str] = Field(
        default_factory=list,
        min_length=0,
        max_length=8,
    )
    discovery_interval_hours: int = Field(default=24, ge=6, le=168)
    max_candidates_per_run: int = Field(default=5, ge=1, le=10)
    allow_proactive_post: bool = False
    placement_policy: PlacementPolicy = Field(default_factory=PlacementPolicy)
    profile_bio: str = Field(default="", max_length=70)
    allow_leave_restricted: bool = False
    allow_join: bool = True
    allow_reply: bool = True
    allow_promotion: bool = True

    @model_validator(mode="before")
    @classmethod
    def read_legacy_switches(cls, data):
        if not isinstance(data, dict):
            return data
        values = dict(data)
        for field, switch in (("max_joins_per_day", "allow_join"),
                              ("max_replies_per_day", "allow_reply"),
                              ("max_promotions_per_day", "allow_promotion"),
                              ("max_promotions_per_week", "allow_promotion")):
            old = values.pop(field, None)
            if old == 0:
                values[switch] = False
        values.pop("reply_limit_scope", None)
        return values


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=120)
    telegram_url: str = Field(max_length=180)
    source_url: str = Field(max_length=2000)
    reading_need: str = Field(max_length=400)
    rules_summary: str = Field(max_length=600)
    rules_quote: str = Field(default="", max_length=350)
    scenario_quote: str = Field(default="", max_length=250)
    audience_lane: Literal["language", "travel", "collaboration", "tools", "interests", "social", "unknown"] = "unknown"
    language: str = Field(default="unknown", pattern=r"^(unknown|[a-z]{2,3}(?:-[A-Za-z]{2,8})?)$")
    group_kind: Literal["discussion", "broadcast", "unknown"] = "unknown"
    label_quote: str = Field(default="", max_length=200)


class Discovery(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list, max_length=10)
    summary: str = Field(max_length=800)


def discovery_config(c, agent):
    """A connected channel can research public entry points. Participation is explicit."""
    saved = agent["onboarding_state"].get("community_growth")
    if saved is not None:
        return GrowthConfig.model_validate(saved)
    connected = one(
        c,
        """SELECT 1 FROM lead.social_accounts WHERE platform='telegram'
        AND state='connected' AND automation_enabled AND agent_id=%s LIMIT 1""",
        (agent["id"],),
    )
    return GrowthConfig(enabled=bool(connected), mode="discovery")


def get_strategy(aid):
    with tx(aid) as c:
        a = require_agent(c, aid)
        config = discovery_config(c, a)
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
            "围绕当前产品寻找相关社群，核对讨论入口和公开规则。",
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


def verified_candidates(c, output, sources, *, require_rule_quote=False, chamber=False, widen=False):
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
        native_group = next(
            (
                group
                for group in observed.get("groups", [])
                if group["telegram_url"].casefold() == candidate.telegram_url.casefold()
            ),
            None,
        )
        card = urlsplit(source["url"]).hostname == "t.me"
        if card and (
            not observed.get("is_public_group")
            or source["url"].casefold() != candidate.telegram_url.casefold()
        ):
            continue
        from .public_telegram import audience_counts

        data = candidate.model_dump()
        subtitle = observed.get("subtitle") if card else (native_group or {}).get("subtitle")
        data.update(audience_counts(subtitle))
        public_topic = ((observed["title"] + " " + (observed.get("description") or ""))
                        if card else (native_group or {}).get("name", "") + " " + ((native_group or {}).get("subtitle") or ""))
        label_verified = bool(candidate.label_quote.strip()) and candidate.label_quote.strip().casefold() in public_topic.casefold()
        if not label_verified:
            data.update(audience_lane="unknown", language="unknown", group_kind="unknown", label_quote="")
        data["label_version"] = 1
        data["label_evidence_id"] = str(source["evidence_id"]) if label_verified else None
        if chamber:
            public_topic = (
                (observed["title"] + " " + (observed.get("description") or ""))
                if card else (native_group or {}).get("name", "")
            )
            if excluded_group_topic(public_topic):
                continue
            if data["audience_lane"] == "unknown":
                inferred = audience_lane(public_topic)
                data["audience_lane"] = inferred if inferred != "other" else "unknown"
            priority = chamber_scenario_priority(public_topic)
            quote = candidate.scenario_quote.strip()
            observe = not priority and observation_topic(public_topic, data, widen=widen)
            if not priority and (card or native_group) and not observe and candidate.reading_need and data.get("members",0)>=2:
                observe = True
            if observe:
                data["selection_basis"] = "observe_recent_chat"
            elif not priority and quote and quote.casefold() in public_topic.casefold():
                priority = 1
            if not priority and not observe:
                continue
            data["scenario_priority"] = priority
        if card:
            data["name"] = observed["title"]
        if require_rule_quote:
            from .community_discovery import is_search_url

            quote = candidate.rules_quote.strip()
            if (
                card
                and quote
                and quote in (observed.get("rules_text") or "")
                and observed.get("rules_link")
            ):
                data["rules_source_url"] = observed["rules_link"]
            elif (
                not quote
                or is_search_url(source["url"])
                or quote
                not in (
                    (observed.get("description") if card else observed.get("text"))
                    or ""
                )
            ):
                data.update(
                    rules_quote="", rules_summary="公开来源未核实发言和推广链接规则。"
                )
        found.append(
            {
                **data,
                "name": (native_group or {}).get("name") or data["name"],
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
    import asyncio
    from datetime import datetime, timezone

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
    # Each call advances one durable phase. Queue/backoff time is not execution
    # time and cannot manufacture a successful discovery batch.
    import time

    from .community_discovery import advance

    started = time.monotonic()
    with tx(aid) as c:
        checked = one(c, "SELECT now() AS at")["at"]
        batch = one(c, "SELECT id FROM lead.tasks WHERE type='community.discover' AND state='running' ORDER BY created_at LIMIT 1")
    timed_out = False
    try:
        timer = asyncio.timeout(120)
        async with timer:
            return await advance(aid)
    except TimeoutError:
        if not timer.expired():
            raise
        timed_out = True
        return {"delay": 30, "reason": "community_discovery_checkpoint_wait"}
    finally:
        elapsed = round(time.monotonic()-started, 3)
        with tx(aid) as c:
            current = one(c, """SELECT id,input,result FROM lead.tasks WHERE type='community.discover'
                AND (id=%s OR (%s::uuid IS NULL AND created_at>=%s)) ORDER BY created_at LIMIT 1 FOR UPDATE""",
                (batch["id"] if batch else None, batch["id"] if batch else None, checked))
            if current:
                saved = current["result"] or {}
                campaign_id = saved.get("campaign_id") or current["input"].get("continued_from") or str(current["id"])
                c.execute("UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s",
                          (j({"campaign_id": campaign_id, "execution_seconds": round(float(saved.get("execution_seconds", 0))+elapsed, 3),
                              "execution_steps_count": int(saved.get("execution_steps_count", 0))+1,
                              "checkpoint": {"stage": saved.get("phase", "collect"), "state": "timed_out" if timed_out else "completed",
                                             "at": datetime.now(timezone.utc).isoformat()},
                              "slice_timeouts": int(saved.get("slice_timeouts", 0))+int(timed_out)}), current["id"]))


def acquisition_window(c, revision, *, hours=6, chamber=False):
    """Count first discoveries and confirmed memberships in the same rolling window."""
    return one(c, """
        WITH found AS (
            SELECT lower(item->>'telegram_url') AS url,min(t.completed_at) AS first_seen
            FROM lead.tasks t CROSS JOIN LATERAL jsonb_array_elements(t.result->'candidates') item
            WHERE t.type='community.discover' AND t.state='succeeded' AND t.input->>'config_revision'=%s
              AND (NOT %s OR item->>'scenario_priority' IN ('1','2') OR item->>'selection_basis'='observe_recent_chat')
            GROUP BY 1
        ), joined AS (
            SELECT lower(input->'candidate'->>'telegram_url') AS url,
              min(coalesce(completed_at,started_at,created_at)) AS confirmed
            FROM lead.tasks WHERE type='community.engage' AND input->>'config_revision'=%s
              AND result->>'join_state'='confirmed' GROUP BY 1
        )
        SELECT (SELECT count(*) FROM found WHERE first_seen>now()-make_interval(hours=>%s)) AS found,
          (SELECT count(*) FROM joined WHERE confirmed>now()-make_interval(hours=>%s)) AS joined
        """, (str(revision), chamber, str(revision), hours, hours))


def campaign_progress(c, agent):
    from .growth_experiments import chamber_campaign

    fast = chamber_campaign(agent)
    hours = 1 if fast else 6
    counts = acquisition_window(c, agent["config_revision"], hours=hours, chamber=fast)
    if not fast:
        return {"hours": hours, "candidate_goal": 10, "joined_goal": 3, **counts}
    # A click with an unknown outcome and an existing membership are not new applications.
    actions = one(c, """
        WITH receipts AS (
            SELECT lower(input#>>'{candidate,telegram_url}') AS url,
                   min((result->>'join_dispatched_at')::timestamptz) AS sent,
                   min(coalesce((result->>'join_checked_at')::timestamptz,completed_at,started_at,created_at))
                     FILTER (WHERE result->>'join_state'='confirmed') AS confirmed
            FROM lead.tasks WHERE type='community.engage' AND input->>'config_revision'=%s
              AND result->>'join_submitted'='true'
              AND result->>'join_state' IN ('pending_approval','confirmed')
              AND input#>>'{candidate,telegram_url}' IS NOT NULL
            GROUP BY 1
        ) SELECT count(*) FILTER (WHERE sent>now()-interval '1 hour') AS submitted,
                 count(*) FILTER (WHERE confirmed>now()-interval '1 hour') AS joined FROM receipts
        """, (str(agent["config_revision"]),))
    return {"hours": hours, "candidate_goal": None, "submission_goal": None,
            "historical_candidate_reference": 30, **counts, **actions}


def search_interval(c, agent, config):
    from .growth_experiments import chamber_campaign, first_users

    if chamber_campaign(agent):
        return config.discovery_interval_hours
    if first_users(agent):
        window = campaign_progress(c, agent)
        behind = window["found"] < window["candidate_goal"] or (
            config.allow_join and config.mode == "contextual_outreach" and window["joined"] < window["joined_goal"]
        )
        if behind:
            return min(2, config.discovery_interval_hours)
    return config.discovery_interval_hours


def group_inventory(c, revision):
    """One row per encountered group, with platform counts and mature promotion results."""
    return all_rows(c, """
        WITH last_visit AS (
            SELECT DISTINCT ON (lower(input#>>'{candidate,telegram_url}')) id,input,result,created_at
            FROM lead.tasks WHERE type='community.engage' AND input->>'config_revision'=%s
              AND input#>>'{candidate,telegram_url}' IS NOT NULL
            ORDER BY lower(input#>>'{candidate,telegram_url}'),created_at DESC,id DESC
        ), probes AS (
            SELECT DISTINCT floor(extract(epoch FROM started_at)/900)::bigint AS bucket
            FROM lead.visit_sessions WHERE exclusion_reason='coverage_probe'
            AND started_at>=now()-interval '8 days'
        ), sent AS (
            SELECT t.id,lower(t.input#>>'{candidate,telegram_url}') AS url,
                   t.result->>'target_id' AS peer, t.input->>'external_account_id' AS account,
                   coalesce((t.result->>'reply_dispatched_at')::timestamptz,t.completed_at) AS sent_at,
                   l.id AS link,l.state AS link_state
            FROM lead.tasks t JOIN lead.tracking_links l ON l.id::text=t.input#>>'{reply,tracking_link_id}'
            WHERE t.type='community.engage' AND t.input->>'config_revision'=%s
              AND t.result->>'reply_state'='confirmed' AND t.input#>>'{reply,promote}'='true'
        ), placements AS (
            SELECT p.*, count(v.id) FILTER(WHERE v.exclusion_reason IS NULL) AS clicks,
              (p.link_state='active' AND NOT EXISTS (SELECT 1 FROM generate_series(
                  floor(extract(epoch FROM p.sent_at)/900)::bigint,
                  floor(extract(epoch FROM p.sent_at+interval '24 hours')/900)::bigint
              ) buckets(n) WHERE NOT EXISTS(SELECT 1 FROM probes WHERE bucket=buckets.n))) AS measured
            FROM sent p LEFT JOIN lead.visit_sessions v ON v.tracking_link_id=p.link AND v.trust_level='redirect_observed'
              AND v.started_at>=p.sent_at AND v.started_at<p.sent_at+interval '24 hours'
            WHERE p.sent_at BETWEEN now()-interval '7 days' AND now()-interval '24 hours'
            GROUP BY p.id,p.url,p.peer,p.account,p.sent_at,p.link,p.link_state
        )
        SELECT input#>>'{candidate,name}' AS name,input#>>'{candidate,telegram_url}' AS url,
          coalesce((result#>>'{history_observation,members}')::int,(input#>>'{candidate,members}')::int) AS members,
          coalesce((result#>>'{history_observation,online}')::int,(input#>>'{candidate,online}')::int) AS online,
          CASE WHEN result->>'leave_state'='confirmed' THEN 'false' ELSE result#>>'{history_observation,joined}' END AS last_seen_joined,
          result->>'leave_state' AS leave_state,result->>'join_state' AS join_state,
          result#>>'{outcome,reason}' AS latest_outcome,created_at AS last_visit,
          coalesce(placements,0) AS mature_promotions,visits AS recorded_visits, measured AS covered_promotions
        FROM last_visit LEFT JOIN LATERAL (
            SELECT count(*) AS placements, count(*) FILTER(WHERE measured) AS measured,
              CASE WHEN sum(clicks)>0 OR bool_and(measured) THEN sum(clicks)::int END AS visits
            FROM placements p WHERE p.account IS NOT DISTINCT FROM input->>'external_account_id' AND
              (p.url=lower(input#>>'{candidate,telegram_url}') OR p.peer=result->>'target_id')
          ) outcomes ON true
        ORDER BY online DESC NULLS LAST,members DESC NULLS LAST,created_at DESC
        """, (str(revision), str(revision)))
