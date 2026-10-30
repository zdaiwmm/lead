"""Owner-scoped account binding, targets, versioned grants and probe cards."""

import secrets

import psycopg

from .adapters.catalog import SPECS, validate
from .db import append_message, audit, emit, j, one, record, require_agent, tx
from .scope import current_tenant


def disconnect_account(aid, account_id, expected_verified_at):
    """Release an account claim without deleting its operational history."""
    with tx(aid) as c:
        require_agent(c, aid, True)
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE id=%s FOR UPDATE",
            (account_id,),
        )
        if not account or account["platform"] not in SPECS:
            raise LookupError("社媒账号不存在")
        if account["verified_at"] != expected_verified_at:
            raise ValueError("账号连接已更新，请刷新后重新确认解除绑定")
        if account["state"] == "disconnected":
            return {"ok": True, "duplicate": True}
        c.execute(
            "UPDATE lead.social_accounts SET state='disconnected',automation_enabled=false WHERE id=%s",
            (account_id,),
        )
        c.execute(
            "UPDATE lead.authorization_grants SET revoked_at=now() WHERE target_id IN (SELECT id FROM lead.account_targets WHERE account_id=%s) AND revoked_at IS NULL",
            (account_id,),
        )
        c.execute(
            "UPDATE lead.account_targets SET state='disabled' WHERE account_id=%s",
            (account_id,),
        )
        c.execute(
            """UPDATE lead.interaction_cards SET state='canceled'
            WHERE state IN ('pending','accepted') AND
              (payload->>'account_id'=%s OR payload->>'target_id' IN
                (SELECT id::text FROM lead.account_targets WHERE account_id=%s))""",
            (str(account_id), account_id),
        )
        # Submitted/unknown actions retain their receipts and reconciliation state.
        c.execute(
            "UPDATE lead.actions SET state='canceled' WHERE state IN ('prepared','not_applied') AND target_id IN (SELECT id FROM lead.account_targets WHERE account_id=%s)",
            (account_id,),
        )
        c.execute(
            """UPDATE lead.tasks t SET state='canceled',completed_at=now(),version=version+1
            WHERE t.state IN ('planned','queued','running','waiting_user') AND
              (t.input->>'platform'=%s OR t.input->>'account_id'=%s OR
               t.input->>'target_id' IN (SELECT id::text FROM lead.account_targets WHERE account_id=%s) OR
               EXISTS(SELECT 1 FROM lead.actions a JOIN lead.account_targets target ON target.id=a.target_id
                      WHERE a.task_id=t.id AND target.account_id=%s))
              AND NOT EXISTS(SELECT 1 FROM lead.actions a WHERE a.task_id=t.id AND a.state IN ('dispatching','unknown'))""",
            (account["platform"], str(account_id), account_id, account_id),
        )
        # The browser commit gate takes the same agent lock before any submission.
        c.execute(
            "UPDATE lead.agents SET authorization_version=authorization_version+1,control_version=control_version+1,version=version+1,updated_at=now() WHERE id=%s",
            (aid,),
        )
        c.execute(
            "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state IN ('requested','active','ending')"
        )
        audit(
            c,
            aid,
            "account.disconnected",
            {"account_id": str(account_id), "platform": account["platform"]},
        )
        record(c, aid, "已解除社媒账号绑定", account["platform"])
        emit(c, aid, "supervisor.wake")
    return {"ok": True}


def store_account(
    aid, platform, observed, prompt_target=True, *, expected_control_version=None
):
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        if (
            expected_control_version is not None
            and agent["control_version"] != expected_control_version
        ):
            raise ValueError("账号连接已变化，请重新打开登录窗口并验证")
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE platform=%s ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
            (platform,),
        )
        if not account:
            raise ValueError("请先打开此账号的连接窗口")
        if (
            account["external_account_id"]
            and account["external_account_id"] != observed["external_account_id"]
        ):
            c.execute(
                "UPDATE lead.authorization_grants SET revoked_at=now() WHERE target_id IN (SELECT id FROM lead.account_targets WHERE account_id=%s) AND revoked_at IS NULL",
                (account["id"],),
            )
            c.execute(
                "UPDATE lead.account_targets SET state='disabled' WHERE account_id=%s",
                (account["id"],),
            )
            c.execute(
                "UPDATE lead.interaction_cards SET state='canceled' WHERE type='manage_account' AND payload->>'account_id'=%s AND state='accepted'",
                (str(account["id"]),),
            )
            c.execute(
                "UPDATE lead.agents SET authorization_version=authorization_version+1,control_version=control_version+1 WHERE id=%s",
                (aid,),
            )
        try:
            c.execute(
                "UPDATE lead.social_accounts SET external_account_id=%s,display_name=%s,automation_enabled=CASE WHEN state='disconnected' THEN true ELSE automation_enabled END,state='connected',verified_at=now() WHERE id=%s",
                (
                    observed["external_account_id"],
                    observed["display_name"],
                    account["id"],
                ),
            )
        except psycopg.errors.UniqueViolation:
            raise ValueError(
                "该账号已由另一名 Agent 管理，请到原 Agent 的「社媒账号」解除绑定后重试"
            ) from None
        record(c, aid, "已核对登录账号", platform)
        audit(
            c,
            aid,
            "account.verified",
            {"account_id": str(account["id"]), "platform": platform},
        )
        if prompt_target:
            append_message(
                c,
                aid,
                "assistant",
                "Telegram 已登录。启动后我会自动准备运营频道、验证连接并开始运营。"
                if platform == "telegram"
                else platform + " 已登录。把你有运营权限的频道、页面或画板链接发给我。",
                cards=[
                    {
                        "type": "channel_target",
                        "account_id": str(account["id"]),
                        "platform": platform,
                    }
                ],
            )
        return str(account["id"])


def store_target(aid, account_id, observed, *, notify=True):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE id=%s AND state='connected'",
            (account_id,),
        )
        if not account or account["platform"] not in SPECS:
            raise ValueError("账号尚未验证")
        spec = SPECS[account["platform"]]
        target = one(
            c,
            "INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,canonical_url,display_name,verified_permissions,state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'active') ON CONFLICT(account_id,target_type,external_target_id) DO UPDATE SET canonical_url=excluded.canonical_url,display_name=excluded.display_name,verified_permissions=lead.account_targets.verified_permissions || excluded.verified_permissions,state='active' RETURNING *",
            (
                current_tenant(),
                aid,
                account_id,
                observed.get("target_type", spec.target_type),
                observed["external_target_id"],
                observed["canonical_url"],
                observed["display_name"],
                j(
                    {
                        "actions": observed["verified_permissions"],
                        "public_url": observed.get("public_url"),
                        "adapter_version": spec.version,
                    }
                ),
            ),
        )
        if not notify and one(
            c,
            "SELECT id FROM lead.authorization_grants WHERE target_id=%s AND revoked_at IS NULL",
            (target["id"],),
        ):
            return {"target_id": target["id"], "card_id": None}
        existing = one(
            c,
            "SELECT id FROM lead.interaction_cards WHERE type='authorize_target' AND state='pending' AND expires_at>now() AND payload->>'target_id'=%s AND (payload->>'expected_authorization_version')::bigint=%s",
            (str(target["id"]), a["authorization_version"]),
        )
        if existing:
            return {"target_id": target["id"], "card_id": existing["id"]}
        card = one(
            c,
            "INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,payload,expires_at) VALUES(%s,%s,'authorize_target',%s,now()+interval '1 day') RETURNING *",
            (
                current_tenant(),
                aid,
                j(
                    {
                        "target_id": str(target["id"]),
                        "target_name": target["display_name"],
                        "allowed_actions": ["publish", "upload_media"],
                        "formats": list(spec.formats),
                        "limits": {"publish_per_day": 3, "read_others": False},
                        "expected_authorization_version": a["authorization_version"],
                    }
                ),
            ),
        )
        if not notify:
            return {"target_id": target["id"], "card_id": card["id"]}
        append_message(
            c,
            aid,
            "assistant",
            "已核对「"
            + target["display_name"]
            + "」的操作权限。授权后只在此目标发布文字及可用的图片、视频，每天最多 3 条。",
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


def probe_in_tx(
    c,
    aid,
    target,
    grant_id,
    authorization_version,
    asset_ids=(),
    made_for_kids=None,
    ai_generated=None,
    notify=True,
):
    media = []
    records = []
    for asset_id in asset_ids:
        row = one(
            c, "SELECT * FROM lead.assets WHERE id=%s AND state='ready'", (asset_id,)
        )
        if not row:
            raise LookupError("素材不存在")
        if ai_generated is not None:
            c.execute(
                "UPDATE lead.assets SET rights=rights || %s WHERE id=%s",
                (j({"ai_generated": ai_generated}), asset_id),
            )
            row["rights"]["ai_generated"] = ai_generated
        records.append(row)
        media.append(
            {
                "id": str(row["id"]),
                "sha256": row["sha256"],
                "kind": row["metadata"]["kind"],
                "ai_generated": row["rights"].get("ai_generated"),
            }
        )
    marker = "lead-verify-" + secrets.token_hex(6)
    publication = {
        "body": "lead 连接验证 · " + marker,
        "title": "lead 连接验证 " + marker,
        "marker": marker,
        "media": media,
        "made_for_kids": made_for_kids,
        "commercial_disclosure": "own_brand",
    }
    kind = validate(target["platform"], publication, records)
    payload = {
        **publication,
        "format": kind,
        "target_id": str(target["id"]),
        "grant_id": str(grant_id),
        "expected_authorization_version": authorization_version,
    }
    card = one(
        c,
        "INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,payload,expires_at) VALUES(%s,%s,'verify_publish',%s,now()+interval '1 day') RETURNING *",
        (current_tenant(), aid, j(payload)),
    )
    if not notify:
        return {"card_id": card["id"], "version": card["version"]}
    append_message(
        c,
        aid,
        "assistant",
        "下面是首次验证将发布的完整内容。确认后才会上传素材并提交；这条内容不计推广成果。",
        cards=[
            {
                "type": "verify_publish",
                "id": str(card["id"]),
                "version": card["version"],
                "target_name": target["display_name"],
                "body": publication["body"],
                "title": publication["title"],
                "media": media,
            }
        ],
    )
    return {"card_id": card["id"], "version": card["version"]}


def create_verification(
    aid, target_id, asset_ids=(), made_for_kids=None, ai_generated=None
):
    if len(asset_ids) != len(set(asset_ids)) or len(asset_ids) > 10:
        raise ValueError("素材重复或过多")
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        target = one(
            c,
            "SELECT t.*,s.platform FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s AND t.state='active' AND s.state='connected'",
            (target_id,),
        )
        grant = one(
            c,
            "SELECT id FROM lead.authorization_grants WHERE target_id=%s AND revoked_at IS NULL ORDER BY version DESC LIMIT 1",
            (target_id,),
        )
        if not target or not grant:
            raise ValueError("目标尚未授权")
        return probe_in_tx(
            c,
            aid,
            target,
            grant["id"],
            a["authorization_version"],
            asset_ids,
            made_for_kids,
            ai_generated,
        )


def accept_card(
    aid, card_id, version, accepted, *, delegation_id=None, bootstrap_run_id=None
):
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
            raise ValueError("账号或目标状态已改变")
        if delegation_id:
            from .account_setup import require_delegation

            account = one(
                c,
                "SELECT * FROM lead.social_accounts WHERE id=%s",
                (target["account_id"],),
            )
            require_delegation(c, a, account, delegation_id)
        if card["type"] == "authorize_target":
            if a["authorization_version"] != payload["expected_authorization_version"]:
                raise ValueError("授权范围已更新，请重新核对目标")
            ver = a["authorization_version"] + 1
            c.execute(
                "UPDATE lead.authorization_grants SET revoked_at=now() WHERE target_id=%s AND revoked_at IS NULL",
                (target["id"],),
            )
            grant = one(
                c,
                "INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,limits,granted_by) VALUES(%s,%s,%s,%s,%s,%s,'local_owner') RETURNING id",
                (
                    current_tenant(),
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
            spec = SPECS[target["platform"]]
            if "text" in spec.formats:
                probe_in_tx(c, aid, target, grant["id"], ver, notify=not delegation_id)
            else:
                append_message(
                    c,
                    aid,
                    "assistant",
                    "授权已保存。此平台需要媒体，请选择一份有权使用的素材来验证首次发布。",
                    cards=[
                        {
                            "type": "media_verification",
                            "target_id": str(target["id"]),
                            "platform": target["platform"],
                            "formats": list(spec.formats),
                        }
                    ],
                )
        elif card["type"] == "verify_publish":
            if a["authorization_version"] != payload["expected_authorization_version"]:
                raise ValueError("目标授权已变化")
            if not one(
                c,
                "SELECT 1 FROM lead.authorization_grants WHERE id=%s AND target_id=%s AND revoked_at IS NULL",
                (payload["grant_id"], target["id"]),
            ):
                raise ValueError("授权已撤销")
            if one(
                c,
                "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now()",
            ):
                raise ValueError("请先结束浏览器接管")
            records = []
            for item in payload.get("media", []):
                row = one(
                    c,
                    "SELECT * FROM lead.assets WHERE id=%s AND sha256=%s AND state='ready'",
                    (item["id"], item["sha256"]),
                )
                if not row:
                    raise ValueError("素材已变化或不再可用")
                records.append(row)
            validate(target["platform"], payload, records)
            c.execute(
                "UPDATE lead.agents SET desired_state=%s,health_state='queued',control_version=control_version+1 WHERE id=%s",
                (a["desired_state"] if delegation_id else "draft", aid),
            )
            task = one(
                c,
                "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'connection.verify_publish','queued',%s,%s,now()) RETURNING id",
                (
                    current_tenant(),
                    aid,
                    "verify:" + str(card_id),
                    j(
                        {
                            **payload,
                            "key": str(card_id),
                            "bootstrap_verification": True,
                            "bootstrap_run_id": bootstrap_run_id,
                            "control_version": a["control_version"] + 1,
                            "delegation_id": str(delegation_id)
                            if delegation_id
                            else None,
                        }
                    ),
                ),
            )
            emit(c, aid, "task.enqueue", {"task_id": str(task["id"])})
            record(
                c,
                aid,
                "已安排连接验证",
                "仅用于连接验证，不计推广成果。",
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
