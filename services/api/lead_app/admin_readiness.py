"""Read-only workspace readiness from actual configuration and execution evidence."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import admin_config as configs
from .adapters.catalog import SPECS
from .admin_content import library
from .admin_settings import brand_context, task_connection
from .db import all_rows


def overview(c, now=None):
    now = now or datetime.now(timezone.utc)
    pack = library(c)
    agents = all_rows(
        c,
        """SELECT a.id,a.name,a.desired_state,a.health_state,a.onboarding_state,
      b.description,b.product_url FROM lead.agents a LEFT JOIN LATERAL(
      SELECT description,product_url FROM lead.business_profiles WHERE agent_id=a.id ORDER BY revision DESC LIMIT 1)b ON true
      WHERE a.desired_state<>'archived' ORDER BY a.created_at DESC LIMIT 200""",
    )
    accounts = all_rows(
        c,
        """SELECT s.agent_id,s.platform,s.display_name,s.state,s.automation_enabled,
      t.id AS target_id,t.state AS target_state,t.verified_permissions,
      EXISTS(SELECT 1 FROM lead.authorization_grants g WHERE g.target_id=t.id AND g.revoked_at IS NULL AND g.allowed_actions ? 'publish') AS granted,
      EXISTS(SELECT 1 FROM lead.platform_capabilities pc WHERE pc.platform=s.platform AND pc.target_type=t.target_type AND pc.action_type='publish'
      AND pc.technical_state='verified' AND pc.permission_state='approved') AS platform_approved
      FROM lead.social_accounts s LEFT JOIN lead.account_targets t ON t.account_id=s.id WHERE s.platform<>'lab' ORDER BY s.created_at""",
    )
    analytics = all_rows(
        c,
        """SELECT DISTINCT ON(agent_id) agent_id,site_host,state,last_received_at
      FROM lead.analytics_connections WHERE provider='lead_site' ORDER BY agent_id,created_at DESC""",
    )
    credentials = all_rows(
        c,
        "SELECT agent_id,max(last_received_at) AS last_received_at FROM lead.analytics_credentials WHERE state='active' GROUP BY agent_id",
    )
    results = []
    for agent in agents:
        aid = str(agent["id"])
        checks = []

        def check(code, title, ready, detail, section, *, informational=False):
            checks.append(
                {
                    "code": code,
                    "title": title,
                    "state": "ready"
                    if ready
                    else "not_connected"
                    if informational
                    else "needs_attention",
                    "detail": detail,
                    "section": section,
                }
            )

        has_business = bool((agent["description"] or "").strip())
        check(
            "business",
            "业务资料",
            has_business,
            "已有业务介绍。"
            if has_business
            else "尚无业务介绍，请先补充真实产品和目标客户。",
            "workbench",
        )
        missing = []
        models = {}
        for purpose, label in (
            ("chat", "对话"),
            ("planning", "运营规划"),
            ("research", "研究"),
        ):
            connection, reference, version = task_connection(c, aid, purpose)
            models[purpose] = {
                "reference": reference,
                "version": version,
                "credential_configured": bool(connection["key"]),
            }
            if not connection["key"]:
                missing.append(label)
        check(
            "models",
            "模型服务",
            not missing,
            "模型凭据已配置，连接和生成结果仍以实际检查为准。"
            if not missing
            else "缺少服务端凭据：" + "、".join(missing),
            "models",
        )
        brand = brand_context(c, aid, now=now)
        check(
            "brand",
            "品牌与知识",
            bool(brand["name"] and brand["documents"]),
            f"生效资料 {len(brand['documents'])} 条；"
            + ("已设置品牌名称。" if brand["name"] else "仍需设置此业务的品牌名称。"),
            "brand",
        )
        invalid = configs.read(c, "brand/" + aid)["body"]["documents"]
        pending = sum(d["status"] == "pending" for d in invalid)
        expired = sum(
            d["status"] == "approved"
            and bool(d["valid_until"])
            and d["valid_until"]
            < now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            for d in invalid
        )
        if pending or expired:
            check(
                "knowledge_review",
                "资料待处理",
                False,
                f"待确认 {pending} 条，已过期 {expired} 条；这些资料不会进入 Agent 上下文。",
                "brand",
            )
        channels = []
        for account in accounts:
            if str(account["agent_id"]) != aid:
                continue
            permissions = account["verified_permissions"] or {}
            from .admin_controls import channel_policy

            channel_rule, _ = channel_policy(
                c, aid, account["platform"], account["target_id"]
            )
            spec = SPECS.get(account["platform"])
            formats = sorted(
                set(permissions.get("publishing_verified_formats", []))
                & set(spec.formats if spec else ())
            )
            ready = (
                account["state"] == "connected"
                and account["target_state"] == "active"
                and account["granted"]
                and account["platform_approved"]
                and bool(formats)
                and bool(spec and permissions.get("adapter_version") == spec.version)
                and channel_rule["enabled"]
                and channel_rule["allow_publishing"]
                and bool(set(formats) & set(channel_rule["formats"]))
                and channel_rule["daily_publish_limit"] != 0
            )
            reasons = []
            if (
                not channel_rule["enabled"]
                or not channel_rule["allow_publishing"]
                or channel_rule["daily_publish_limit"] == 0
                or not set(formats) & set(channel_rule["formats"])
            ):
                reasons.append("后台渠道规则限制了此目标的发布")
            if account["state"] != "connected":
                reasons.append("需要登录验证")
            if not account["target_id"] or account["target_state"] != "active":
                reasons.append("需要有效运营目标")
            if not account["granted"]:
                reasons.append("尚无发布授权")
            if not account["platform_approved"]:
                reasons.append("平台条件尚未通过")
            if not formats or (
                spec and permissions.get("adapter_version") != spec.version
            ):
                reasons.append("发布格式或适配版本尚未验证")
            channels.append(
                {
                    "platform": account["platform"],
                    "name": account["display_name"],
                    "target_id": account["target_id"],
                    "ready": ready,
                    "automation_enabled": account["automation_enabled"],
                    "verified_formats": formats,
                    "reasons": reasons,
                }
            )
        ready_channels = sum(ch["ready"] for ch in channels)
        check(
            "channels",
            "渠道与账号",
            ready_channels > 0,
            f"{ready_channels} 个目标已通过发布前置检查；暂停状态继续由工作台控制。"
            if ready_channels
            else "尚无通过全部发布前置检查的目标。模板已填充也不会扩大账号授权。",
            "workbench",
        )
        site = next((row for row in analytics if str(row["agent_id"]) == aid), None)
        from .admin_controls import attribution

        measurement, _ = attribution(c, aid)
        freshness_hours = measurement["freshness_hours"]
        server = next((row for row in credentials if str(row["agent_id"]) == aid), None)
        site_fresh = bool(
            site
            and site["state"] == "verified"
            and site["last_received_at"]
            and site["last_received_at"] >= now - timedelta(hours=freshness_hours)
        )
        check(
            "site",
            "网站访问回传",
            site_fresh,
            f"最近 {freshness_hours} 小时收到过真实网站数据。"
            if site_fresh
            else f"尚未接入或最近 {freshness_hours} 小时未收到网站数据，不能据此判断当前获客效果。",
            "workbench",
            informational=True,
        )
        server_fresh = bool(
            server
            and server["last_received_at"]
            and server["last_received_at"] >= now - timedelta(hours=freshness_hours)
        )
        check(
            "conversion",
            "服务端转化回传",
            server_fresh,
            f"最近 {freshness_hours} 小时收到过服务端回传。"
            if server_fresh
            else "尚未收到近期服务端回传，注册、试用和付费结果保留未知。",
            "workbench",
            informational=True,
        )
        results.append(
            {
                "id": aid,
                "name": agent["name"],
                "desired_state": agent["desired_state"],
                "setup_pending": bool(agent["onboarding_state"].get("setup_pending")),
                "checks": checks,
                "channels": channels,
                "models": models,
                "freshness_hours": freshness_hours,
                "site": site,
                "server_last_received_at": server["last_received_at"]
                if server
                else None,
            }
        )
    return {
        "checked_at": now,
        "freshness_days": 7,
        "templates": [
            {
                k: entry[k]
                for k in ("key", "title", "state", "version", "published_version")
            }
            for entry in pack["entries"]
        ],
        "missing_templates": sum(
            entry["published_version"] == 0 for entry in pack["entries"]
        ),
        "agents": results,
        "agent_limit": 200,
    }
