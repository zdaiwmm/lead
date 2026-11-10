"""First-party editorial preparation and measurement for isolated Reddit accounts."""

from datetime import datetime, timedelta

from .account_profiles import isolated_login_platforms
from .admin_operations import work_window
from .admin_settings import effective
from .db import all_rows, j, one, record, tx
from .promotion import promoter_voice
from .scope import current_tenant


def review_workspace(aid):
    if "reddit" not in isolated_login_platforms(aid):
        return
    with tx(aid) as c:
        c.execute("SET LOCAL lock_timeout='2s'")
        agent = one(
            c, "SELECT * FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,)
        )
        if not agent or agent["desired_state"] != "running":
            return
        runtime, _ = effective(c, "runtime", aid)
        if not work_window(runtime["work_hours"])["allowed"]:
            return
        accounts = all_rows(
            c,
            """SELECT s.id,s.binding_version FROM lead.social_accounts s
            JOIN lead.browser_profiles p ON p.id=s.profile_id
            WHERE s.platform='reddit' AND s.state='connected' AND s.automation_enabled
              AND (p.slot='reddit' OR starts_with(p.slot,'reddit-')) AND NOT EXISTS (SELECT 1 FROM lead.takeover_sessions t
              WHERE t.profile_id=p.id AND t.expires_at>now() AND t.state IN ('requested','active','ending'))
            ORDER BY s.id""",
        )
        if not accounts:
            return
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if not business or not business["description"].strip():
            return
        now = one(c, "SELECT now() AS clock")["clock"]
        voice = "independent_promoter" if promoter_voice(agent) else "project_operator"
        for account in accounts:
            scope = {
                "platform": "reddit",
                "editorial_version": 2,
                "public_voice": voice,
                "account_id": str(account["id"]),
                "binding_version": account["binding_version"],
                "business_profile_id": str(business["id"]),
                "config_revision": agent["config_revision"],
            }
            previous = one(
                c,
                """SELECT result FROM lead.tasks WHERE type='reddit.workspace_review'
                AND state='succeeded' AND input @> %s ORDER BY completed_at DESC LIMIT 1""",
                (j(scope),),
            )
            if (
                previous
                and datetime.fromisoformat(previous["result"]["next_check_at"]) > now
            ):
                continue
            counts = one(
                c,
                """SELECT count(DISTINCT l.id) AS links,
                    count(v.id) FILTER (WHERE v.trust_level='redirect_observed' AND v.exclusion_reason IS NULL) AS clicks
                FROM lead.tracking_links l LEFT JOIN lead.visit_sessions v ON v.tracking_link_id=l.id
                    AND v.started_at>now()-interval '7 days'
                WHERE l.platform='reddit' AND l.account_id=%s""",
                (account["id"],),
            )
            published = one(
                c,
                """SELECT count(*) AS n FROM lead.actions a
                JOIN lead.account_targets t ON t.id=a.target_id JOIN lead.tasks task ON task.id=a.task_id
                WHERE t.account_id=%s AND task.type='reddit.publish' AND a.state='confirmed' AND a.confirmed_at>now()-interval '7 days'
                AND coalesce(task.input->>'bootstrap_verification','false')<>'true'""",
                (account["id"],),
            )["n"]
            result = {
                **counts,
                "confirmed_posts": published,
                "window_days": 7,
                "next_check_at": (now + timedelta(days=1)).isoformat(),
            }
            # Draft once per source revision. Keep it outside the publishable content queue.
            if not previous:
                name = str(business["facts"].get("name") or agent["name"])
                fact = next(
                    line.strip()
                    for line in business["description"].splitlines()
                    if line.strip()
                )
                disclosure = (
                    f"这份反馈邀请由协助{name}推广的自动化运营助手整理。"
                    if voice == "independent_promoter"
                    else f"这份{name}体验反馈邀请由项目的自动化运营助手整理。"
                )
                question = f"第一次体验{name}时，哪一步最难理解？你原本预期会发生什么？"
                result["editorial_brief"] = {
                    "product": name,
                    "source_excerpt": fact,
                    "product_url": business.get("product_url"),
                    "feedback_question": question,
                    "disclosure": disclosure,
                    "title": f"{name} 的首次体验，有哪一步可以做得更清楚？",
                    "body": f"{disclosure}如果你愿意试用，请选一个你平时会做的操作，先记下预期，再用示例资料走一遍。{question}欢迎指出具体步骤，请勿贴出个人资料。",
                    "use": "供普通社区成员讨论的内部选题。发布前核对该社区当前的分享规则和自动化权限。",
                }
            elif previous["result"].get("editorial_brief"):
                result["editorial_brief"] = previous["result"]["editorial_brief"]
            title = (
                "已整理 Reddit 社区反馈选题"
                if not previous
                else "已复核 Reddit 渠道效果"
            )
            key = f"reddit-workspace:{account['id']}:{account['binding_version']}:{business['id']}:{agent['config_revision']}:v2:{voice}:{now.date()}"
            row = one(
                c,
                """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,started_at,completed_at)
                VALUES(%s,%s,'reddit.workspace_review','succeeded',%s,%s,%s,now(),now(),now())
                ON CONFLICT(workflow_id) DO NOTHING RETURNING id""",
                (current_tenant(), aid, key, j(scope), j(result)),
            )
            if row:
                record(
                    c,
                    aid,
                    title,
                    f"近 7 天有 {published} 条已确认发布、{counts['clicks']} 次归因点击。",
                    task_id=row["id"],
                )


def review_enabled(tenant):
    from .config import env
    from .scope import tenant_scope

    with tenant_scope(tenant, None), tx() as c:
        ids = [
            part.strip()
            for part in env().get("LEAD_SPLIT_REDDIT_LOGIN_AGENT_IDS", "").split(",")
            if part.strip()
        ]
        if not ids:
            return
        agents = all_rows(
            c,
            "SELECT id FROM lead.agents WHERE desired_state='running' AND id::text=ANY(%s)",
            (ids,),
        )
    with tenant_scope(tenant, None):
        for agent in agents:
            review_workspace(str(agent["id"]))
