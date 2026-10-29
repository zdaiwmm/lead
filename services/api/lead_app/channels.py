"""Account, target and one-time scope grants. Models cannot issue these grants."""

import secrets

import psycopg

from .config import TENANT
from .db import (
    append_message,
    audit,
    emit,
    j,
    one,
    record,
    require_agent,
    tx,
)


def store_account(aid, platform, observed):
    with tx(aid) as c:
        require_agent(c, aid, True)
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE platform=%s ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
            (platform,),
        )
        if not account:
            raise ValueError("请先打开此账号的连接窗口")
        try:
            c.execute(
                "UPDATE lead.social_accounts SET external_account_id=%s,display_name=%s,state='connected',verified_at=now() WHERE id=%s",
                (
                    observed["external_account_id"],
                    observed["display_name"],
                    account["id"],
                ),
            )
        except psycopg.errors.UniqueViolation:
            raise ValueError("该账号已由一个 Agent 管理，请先解除原绑定") from None
        record(c, aid, "已验证 Telegram 登录账号")
        audit(
            c,
            aid,
            "account.verified",
            {"account_id": str(account["id"]), "platform": platform},
        )
        append_message(
            c,
            aid,
            "assistant",
            "Telegram 已登录。把你要运营的自有频道链接发给我，我会核对频道和发布权限。",
            cards=[
                {
                    "type": "channel_target",
                    "account_id": str(account["id"]),
                    "platform": "telegram",
                }
            ],
        )
        return str(account["id"])


def store_target(aid, account_id, observed):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE id=%s AND platform='telegram' AND state='connected'",
            (account_id,),
        )
        if not account:
            raise ValueError("账号尚未验证")
        target = one(
            c,
            "INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,canonical_url,display_name,verified_permissions,state) VALUES(%s,%s,%s,'channel',%s,%s,%s,%s,'active') ON CONFLICT(account_id,target_type,external_target_id) DO UPDATE SET canonical_url=excluded.canonical_url,display_name=excluded.display_name,verified_permissions=lead.account_targets.verified_permissions || excluded.verified_permissions,state='active' RETURNING *",
            (
                TENANT,
                aid,
                account_id,
                observed["external_target_id"],
                observed["canonical_url"],
                observed["display_name"],
                j(
                    {
                        "actions": observed["verified_permissions"],
                        "public_url": observed.get("public_url"),
                        "adapter_version": "telegram-webk-1",
                    }
                ),
            ),
        )
        pending = one(
            c,
            "SELECT id FROM lead.interaction_cards WHERE type='authorize_target' AND state='pending' AND expires_at>now() AND payload->>'target_id'=%s AND (payload->>'expected_authorization_version')::bigint=%s",
            (str(target["id"]), a["authorization_version"]),
        )
        if pending:
            return {"target_id": target["id"], "card_id": pending["id"]}
        card = one(
            c,
            "INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,payload,expires_at) VALUES(%s,%s,'authorize_target',%s,now()+interval '1 day') RETURNING *",
            (
                TENANT,
                aid,
                j(
                    {
                        "target_id": str(target["id"]),
                        "target_name": target["display_name"],
                        "allowed_actions": ["publish"],
                        "limits": {"publish_per_day": 3, "read_others": False},
                        "expected_authorization_version": a["authorization_version"],
                    }
                ),
            ),
        )
        append_message(
            c,
            aid,
            "assistant",
            "已核对频道「"
            + target["display_name"]
            + "」的发布权限。授权后，我只会在这个频道发布，每天最多 3 条。",
            cards=[
                {
                    "type": "authorize",
                    "id": str(card["id"]),
                    "version": card["version"],
                    "title": target["display_name"],
                }
            ],
        )
        return {"target_id": target["id"], "card_id": card["id"]}


def accept_card(aid, card_id, version, accepted):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        card = one(
            c,
            "SELECT *,expires_at>now() AS valid FROM lead.interaction_cards WHERE id=%s FOR UPDATE",
            (card_id,),
        )
        if not card:
            raise LookupError("操作卡片不存在")
        if card["version"] != version or not card["valid"]:
            raise ValueError("操作卡片已过期，请重新连接目标")
        if card["state"] == "accepted":
            return {"ok": True, "duplicate": True}
        if card["state"] != "pending":
            raise ValueError("操作已取消")
        if not accepted:
            c.execute(
                "UPDATE lead.interaction_cards SET state='canceled' WHERE id=%s",
                (card_id,),
            )
            return {"ok": True}
        payload = card["payload"]
        target = one(
            c,
            "SELECT t.*,s.platform,s.state AS account_state FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s AND t.state='active'",
            (payload["target_id"],),
        )
        if not target or target["account_state"] != "connected":
            raise ValueError("账号或频道状态已改变")
        if card["type"] == "authorize_target":
            if a["authorization_version"] != payload["expected_authorization_version"]:
                raise ValueError("授权范围已更新，请重新核对频道")
            ver = a["authorization_version"] + 1
            c.execute(
                "UPDATE lead.authorization_grants SET revoked_at=now() WHERE target_id=%s AND revoked_at IS NULL",
                (target["id"],),
            )
            grant = one(
                c,
                "INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,limits,granted_by) VALUES(%s,%s,%s,%s,%s,%s,'local_owner') RETURNING id",
                (
                    TENANT,
                    aid,
                    target["id"],
                    ver,
                    j(payload["allowed_actions"]),
                    j(payload["limits"]),
                ),
            )
            c.execute(
                "UPDATE lead.agents SET authorization_version=%s,control_version=control_version+1 WHERE id=%s",
                (ver, aid),
            )
            marker = "lead-verify-" + secrets.token_hex(6)
            probe = one(
                c,
                "INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,payload,expires_at) VALUES(%s,%s,'verify_publish',%s,now()+interval '1 day') RETURNING *",
                (
                    TENANT,
                    aid,
                    j(
                        {
                            "target_id": str(target["id"]),
                            "grant_id": str(grant["id"]),
                            "body": "lead 频道连接验证 · " + marker,
                            "marker": marker,
                            "expected_authorization_version": ver,
                        }
                    ),
                ),
            )
            append_message(
                c,
                aid,
                "assistant",
                "授权已保存。首次连接需要在此频道验证一次真实发布，下面是将发送的完整内容。",
                cards=[
                    {
                        "type": "verify_publish",
                        "id": str(probe["id"]),
                        "version": probe["version"],
                        "body": probe["payload"]["body"],
                        "target_name": target["display_name"],
                    }
                ],
            )
        elif card["type"] == "verify_publish":
            if a["authorization_version"] != payload["expected_authorization_version"]:
                raise ValueError("频道授权已变化")
            if one(
                c,
                "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now()",
            ):
                raise ValueError("请先结束浏览器接管")
            c.execute(
                "UPDATE lead.agents SET desired_state='draft',health_state='queued',control_version=control_version+1 WHERE id=%s",
                (aid,),
            )
            task = one(
                c,
                "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'connection.verify_publish','queued',%s,%s,now()) RETURNING id",
                (
                    TENANT,
                    aid,
                    "verify:" + str(card_id),
                    j(
                        {
                            **payload,
                            "key": str(card_id),
                            "bootstrap_verification": True,
                            "control_version": a["control_version"] + 1,
                        }
                    ),
                ),
            )
            emit(c, aid, "task.enqueue", {"task_id": str(task["id"])})
            record(
                c,
                aid,
                "已安排频道连接验证",
                "仅用于连接验证，不计作推广成果。",
                task_id=task["id"],
            )
        else:
            raise ValueError("不支持的操作卡片")
        c.execute(
            "UPDATE lead.interaction_cards SET state='accepted' WHERE id=%s", (card_id,)
        )
        audit(
            c,
            aid,
            "card.accepted",
            {
                "card_id": str(card_id),
                "type": card["type"],
                "target_id": str(target["id"]),
            },
        )
        return {"ok": True}
