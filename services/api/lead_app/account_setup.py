"""Owner-delegated Telegram preparation, persisted across supervisor retries.

The model cannot create delegation. Only the owner's start/resume command does.
Channel creation has a committed dispatch barrier; an uncertain creation is only
reconciled. Existing publish grants, evidence and pause fencing still apply.
"""

from .db import all_rows, audit, emit, j, one, record, require_agent, tx, uid
from .scope import current_tenant


def delegate_accounts(c, aid, platform=None):
    for account in all_rows(
        c,
        "SELECT * FROM lead.social_accounts WHERE state='connected' AND automation_enabled AND platform='telegram' AND (%s::text IS NULL OR platform=%s)",
        (platform, platform),
    ):
        if one(
            c,
            "SELECT id FROM lead.interaction_cards WHERE type='manage_account' AND state='accepted' AND expires_at>now() AND payload->>'account_id'=%s AND payload->>'external_account_id'=%s",
            (str(account["id"]), account["external_account_id"]),
        ):
            continue
        card = one(
            c,
            "INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,state,payload,expires_at) VALUES(%s,%s,'manage_account','accepted',%s,now()+interval '10 years') RETURNING id",
            (
                current_tenant(),
                aid,
                j(
                    {
                        "account_id": str(account["id"]),
                        "external_account_id": account["external_account_id"],
                        "allowed_actions": [
                            "create_channel",
                            "publish",
                            "upload_media",
                        ],
                        "limits": {"publish_per_day": 3, "read_others": False},
                        "source": "owner_start",
                    }
                ),
            ),
        )
        audit(
            c,
            aid,
            "account.delegated",
            {"card_id": str(card["id"]), "account_id": str(account["id"])},
        )


def require_delegation(c, agent, account, delegation_id):
    card = one(
        c,
        "SELECT * FROM lead.interaction_cards WHERE id=%s AND type='manage_account' AND state='accepted' AND expires_at>now()",
        (delegation_id,),
    )
    if (
        agent["desired_state"] != "running"
        or not account
        or account["state"] != "connected"
        or not account["automation_enabled"]
        or account["platform"] != "telegram"
        or not card
        or card["payload"].get("account_id") != str(account["id"])
        or card["payload"].get("external_account_id") != account["external_account_id"]
        or card["payload"].get("source") != "owner_start"
        or not {"create_channel", "publish", "upload_media"}.issubset(
            card["payload"].get("allowed_actions", [])
        )
    ):
        raise ValueError("账号运营授权已变化，筹备已停止")
    return card


def request_fresh_channel(aid, request_id):
    """Owner command only: durable, idempotent product-owned channel launch."""
    from uuid import UUID

    request_id = str(UUID(str(request_id)))
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        key = "account.prepare:fresh:" + aid + ":" + request_id
        previous = one(
            c, "SELECT id,state FROM lead.tasks WHERE workflow_id=%s", (key,)
        )
        if previous:
            return {
                "task_id": str(previous["id"]),
                "state": previous["state"],
                "duplicate": True,
            }
        if a["desired_state"] != "running":
            raise ValueError("请先启动 Agent，再安排新频道")
        if one(
            c,
            "SELECT 1 FROM lead.tasks WHERE input->>'fresh_channel'='true' AND (state IN ('queued','running','waiting_user') OR result->>'awaiting_first_publish'='true')",
        ):
            raise ValueError("已有新频道任务正在处理，请先查看它的进度")
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE platform='telegram' AND state='connected' AND automation_enabled ORDER BY created_at LIMIT 1",
        )
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if not account or not business:
            raise ValueError("需要已连接的 Telegram 账号和业务资料")
        delegate_accounts(c, aid, "telegram")
        card = one(
            c,
            "SELECT id FROM lead.interaction_cards WHERE type='manage_account' AND state='accepted' AND expires_at>now() AND payload->>'account_id'=%s AND payload->>'external_account_id'=%s",
            (str(account["id"]), account["external_account_id"]),
        )
        require_delegation(c, a, account, card["id"])
        number = (
            one(
                c,
                "SELECT count(*) AS n FROM lead.tasks WHERE input->>'fresh_channel'='true'",
            )["n"]
            + 1
        )
        handle = (
            "lead_"
            + str(account["id"]).replace("-", "")[:10]
            + "_"
            + request_id.replace("-", "")[:10]
        )
        payload = {
            "fresh_channel": True,
            "source": "owner_product_api",
            "request_id": request_id,
            "account_id": str(account["id"]),
            "external_account_id": account["external_account_id"],
            "delegation_id": str(card["id"]),
            "control_version": a["control_version"],
            "config_revision": a["config_revision"],
            "title": a["name"][:90]
            + " 官方频道"
            + (f" · {number}" if number > 1 else ""),
            "handle": handle,
            "about": a["name"][:40]
            + " 官方频道，由 AI 助手维护。\n"
            + business["description"][:125]
            + "\nhttps://t.me/"
            + handle,
        }
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'account.prepare','queued',%s,%s,now()) RETURNING id,state",
            (current_tenant(), aid, key, j(payload)),
        )
        audit(
            c,
            aid,
            "telegram.fresh_channel_requested",
            {"task_id": str(task["id"]), "request_id": request_id},
        )
        record(
            c,
            aid,
            "已安排新频道筹备",
            "Agent 将完成创建、公开设置、连接验证和首篇发布。",
            task_id=task["id"],
        )
        emit(c, aid, "supervisor.wake")
        return {"task_id": str(task["id"]), "state": task["state"]}


def launch_status(aid, task_id):
    with tx(aid) as c:
        require_agent(c, aid)
        task = one(
            c,
            "SELECT * FROM lead.tasks WHERE id=%s AND type='account.prepare' AND input->>'fresh_channel'='true'",
            (task_id,),
        )
        if not task:
            raise LookupError("新频道任务不存在")
        result = task["result"] or {}
        target = (
            one(
                c,
                "SELECT id,external_target_id,canonical_url,display_name,verified_permissions FROM lead.account_targets WHERE id=%s",
                (result["target_id"],),
            )
            if result.get("target_id")
            else None
        )
        actions = all_rows(
            c,
            "SELECT t.id AS task_id,t.type,t.state,a.id AS action_id,a.state AS action_state,a.external_id,a.external_url,a.evidence_ref,t.result,a.confirmed_at FROM lead.tasks t LEFT JOIN lead.actions a ON a.task_id=t.id WHERE t.input->>'bootstrap_run_id'=%s ORDER BY t.created_at",
            (str(task_id),),
        )
        takeovers = one(
            c,
            "SELECT count(*) AS n FROM lead.takeover_sessions WHERE created_at>=%s",
            (task["created_at"],),
        )["n"]
        agent = one(
            c,
            "SELECT desired_state,health_state,heartbeat_at FROM lead.agents WHERE id=%s",
            (aid,),
        )
        return {
            "task_id": str(task["id"]),
            "state": task["state"],
            "created_at": task["created_at"],
            "title": task["input"]["title"],
            "public_url": "https://t.me/" + task["input"]["handle"],
            "result": result,
            "target": target,
            "publications": actions,
            "takeovers_since_start": takeovers,
            "agent": agent,
            "executor": "product_supervisor_and_browser_gateway",
        }


def recheck_launch(aid, task_id):
    with tx(aid) as c:
        require_agent(c, aid, True)
        parent = one(
            c,
            "SELECT id FROM lead.tasks WHERE id=%s AND type='account.prepare' AND input->>'fresh_channel'='true'",
            (task_id,),
        )
        if not parent:
            raise LookupError("新频道任务不存在")
        children = all_rows(
            c,
            "SELECT t.id FROM lead.tasks t JOIN lead.actions a ON a.task_id=t.id WHERE t.input->>'bootstrap_run_id'=%s AND t.state='reconciling' AND a.state='unknown'",
            (task_id,),
        )
        for child in children:
            emit(
                c,
                aid,
                "task.reconcile",
                {"task_id": str(child["id"]), "recovery_id": uid()},
            )
        emit(c, aid, "supervisor.wake")
        audit(
            c,
            aid,
            "telegram.launch_recheck_requested",
            {"task_id": task_id, "existing_messages_only": True},
        )
        return {"ok": True, "reconciliations": len(children)}


async def prepare_requested_channel(aid, rpc):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        task = one(
            c,
            "SELECT * FROM lead.tasks WHERE type='account.prepare' AND input->>'fresh_channel'='true' AND state IN ('queued','running','waiting_user') ORDER BY created_at LIMIT 1",
        )
        if not task or a["desired_state"] != "running":
            return None
        if one(
            c,
            "SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()",
        ) or one(
            c,
            "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('requested','active','ending') AND expires_at>now()",
        ):
            return {"delay": 60, "reason": "account_preparation_exclusive_access"}
        if (
            task["state"] == "waiting_user"
            and task["input"]["control_version"] == a["control_version"]
        ):
            return {"delay": 600, "reason": "account_preparation_blocked"}
        if one(
            c,
            "SELECT 1 FROM lead.tasks WHERE type<>'account.prepare' AND NOT(type='community.discover' AND coalesce(input->>'mode','')='discovery' AND NOT(input ? 'account_id')) AND state IN ('queued','running','reconciling')",
        ):
            return {"delay": 60, "reason": "finishing_existing_task"}
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE id=%s",
            (task["input"]["account_id"],),
        )
        require_delegation(c, a, account, task["input"]["delegation_id"])
        if account["external_account_id"] != task["input"]["external_account_id"]:
            raise ValueError("账号身份已变化")
        account["delegation_id"] = task["input"]["delegation_id"]
        task = one(
            c,
            "UPDATE lead.tasks SET state='running',started_at=coalesce(started_at,now()),input=input || %s WHERE id=%s RETURNING *",
            (
                j(
                    {
                        "control_version": a["control_version"],
                        "config_revision": a["config_revision"],
                    }
                ),
                task["id"],
            ),
        )
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.agents SET health_state='working',heartbeat_at=now() WHERE id=%s AND desired_state='running'",
            (aid,),
        )
    result = await rpc(
        "/agents/" + aid + "/account/prepare", {"task_id": str(task["id"])}
    )
    if result["state"] != "confirmed":
        return {
            "delay": 60 if result["state"] == "unknown" else 600,
            "reason": "account_preparation_" + result["state"],
        }
    return await finish_preparation(aid, account, task, result["target"])


async def prepare_accounts(aid, rpc):
    """One bounded preparation step per supervisor turn, before content planning."""

    with tx(aid) as c:
        a = one(c, "SELECT * FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,))
        if not a or a["desired_state"] != "running":
            return None
        if one(
            c,
            "SELECT 1 FROM lead.tasks WHERE type='connection.verify_publish' AND state='waiting_user' AND (input->>'control_version')::bigint=%s",
            (a["control_version"],),
        ):
            return {"delay": 3600, "reason": "account_verification_blocked"}
        if one(
            c,
            "SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()",
        ) or one(
            c,
            "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('requested','active','ending') AND expires_at>now()",
        ):
            return None
        if one(
            c,
            "SELECT 1 FROM lead.tasks WHERE type<>'account.prepare' AND NOT(type='community.discover' AND coalesce(input->>'mode','')='discovery' AND NOT(input ? 'account_id')) AND state IN ('queued','running','reconciling')",
        ):
            return None
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if not business:
            return None
        account = one(
            c,
            """SELECT s.*, card.id AS delegation_id FROM lead.social_accounts s
            JOIN lead.interaction_cards card ON card.type='manage_account' AND card.state='accepted'
              AND card.expires_at>now() AND card.payload->>'account_id'=s.id::text
              AND card.payload->>'external_account_id'=s.external_account_id
            WHERE s.platform='telegram' AND s.state='connected' AND s.automation_enabled
              AND NOT EXISTS (SELECT 1 FROM lead.account_targets t JOIN lead.authorization_grants g ON g.target_id=t.id
                JOIN lead.platform_capabilities pc ON pc.platform=s.platform AND pc.target_type=t.target_type AND pc.action_type='publish'
                WHERE t.account_id=s.id AND t.state='active' AND g.revoked_at IS NULL
                  AND g.allowed_actions ? 'publish' AND pc.technical_state='verified' AND pc.permission_state='approved'
                  AND t.verified_permissions->>'adapter_version'=pc.adapter_version
                  AND jsonb_array_length(coalesce(t.verified_permissions->'publishing_verified_formats','[]'))>0)
            ORDER BY s.created_at LIMIT 1""",
        )
        if not account:
            return None
        require_delegation(c, a, account, account["delegation_id"])
        previous = one(
            c,
            "SELECT * FROM lead.tasks WHERE workflow_id=%s",
            (
                "account.prepare:"
                + str(account["id"])
                + ":"
                + account["external_account_id"],
            ),
        )
        # A permanent UI failure is retried after an explicit resume, not every minute.
        if (
            previous
            and previous["state"] == "waiting_user"
            and previous["input"]["control_version"] == a["control_version"]
        ):
            return {"delay": 3600, "reason": "account_preparation_blocked"}
        handle = "lead_" + str(account["id"]).replace("-", "")[:20]
        payload = {
            "account_id": str(account["id"]),
            "external_account_id": account["external_account_id"],
            "delegation_id": str(account["delegation_id"]),
            "control_version": a["control_version"],
            "config_revision": a["config_revision"],
            "title": a["name"][:110] + " 官方动态",
            "handle": handle,
            "about": business["description"][:190] + "\nhttps://t.me/" + handle,
        }
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at,started_at) VALUES(%s,%s,'account.prepare','running',%s,%s,now(),now()) ON CONFLICT(workflow_id) DO UPDATE SET state='running',input=excluded.input,completed_at=NULL RETURNING *",
            (
                current_tenant(),
                aid,
                "account.prepare:"
                + str(account["id"])
                + ":"
                + account["external_account_id"],
                j(
                    {
                        **payload,
                        **(
                            {
                                k: previous["input"][k]
                                for k in ("title", "handle", "about")
                            }
                            if previous
                            else {}
                        ),
                    }
                ),
            ),
        )
        if not previous:
            record(
                c,
                aid,
                "正在准备 Telegram 运营频道",
                "由 Agent 完成频道设置和连接验证。",
                task_id=task["id"],
            )
        c.execute(
            "UPDATE lead.agents SET health_state='working',heartbeat_at=now() WHERE id=%s",
            (aid,),
        )
        target = one(
            c,
            "SELECT * FROM lead.account_targets WHERE account_id=%s AND state='active' ORDER BY id LIMIT 1",
            (account["id"],),
        )
    if target:
        observed = await rpc(
            "/agents/" + aid + "/target/inspect",
            {"account_id": str(account["id"]), "url": target["canonical_url"]},
        )
    else:
        result = await rpc(
            "/agents/" + aid + "/account/prepare", {"task_id": str(task["id"])}
        )
        if result["state"] != "confirmed":
            return {
                "delay": 60 if result["state"] == "unknown" else 600,
                "reason": "account_preparation_" + result["state"],
            }
        observed = result["target"]
    return await finish_preparation(aid, account, task, observed)


async def finish_preparation(aid, account, task, observed):
    from .channels import accept_card, probe_in_tx, store_target

    with tx(aid) as c:
        require_delegation(
            c,
            require_agent(c, aid, True),
            one(c, "SELECT * FROM lead.social_accounts WHERE id=%s", (account["id"],)),
            account["delegation_id"],
        )
    if task["input"].get("fresh_channel"):
        with tx(aid) as c:
            child = one(
                c,
                "SELECT * FROM lead.tasks WHERE input->>'bootstrap_run_id'=%s AND type='connection.verify_publish' AND state NOT IN ('canceled','failed') ORDER BY created_at DESC LIMIT 1",
                (str(task["id"]),),
            )
            if child:
                c.execute(
                    "UPDATE lead.tasks SET state='succeeded',completed_at=coalesce(completed_at,now()),result=coalesce(result,'{}') || %s WHERE id=%s",
                    (
                        j(
                            {
                                "target_id": child["input"]["target_id"],
                                "verification_card_id": child["input"]["key"],
                                "awaiting_first_publish": True,
                                "acceptance_state": "awaiting_first_publish"
                                if child["state"] == "succeeded"
                                else "awaiting_verification",
                            }
                        ),
                        task["id"],
                    ),
                )
                return {"delay": 60, "reason": "account_verifying"}
    binding = store_target(aid, str(account["id"]), observed, notify=False)
    if binding["card_id"]:
        with tx(aid) as c:
            card = one(
                c,
                "SELECT version FROM lead.interaction_cards WHERE id=%s",
                (binding["card_id"],),
            )
        accept_card(
            aid,
            str(binding["card_id"]),
            card["version"],
            True,
            delegation_id=str(account["delegation_id"]),
        )
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        if task["input"].get("fresh_channel"):
            c.execute(
                "UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s",
                (
                    j(
                        {
                            "target_id": str(binding["target_id"]),
                            "awaiting_first_publish": True,
                            "acceptance_state": "awaiting_verification",
                        }
                    ),
                    task["id"],
                ),
            )
        require_delegation(
            c,
            a,
            one(c, "SELECT * FROM lead.social_accounts WHERE id=%s", (account["id"],)),
            account["delegation_id"],
        )
        probe = one(
            c,
            "SELECT * FROM lead.interaction_cards WHERE type='verify_publish' AND state='pending' AND expires_at>now() AND payload->>'target_id'=%s AND (payload->>'expected_authorization_version')::bigint=%s ORDER BY created_at DESC LIMIT 1",
            (str(binding["target_id"]), a["authorization_version"]),
        )
        if not probe:
            target = one(
                c,
                "SELECT t.*,s.platform FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s",
                (binding["target_id"],),
            )
            grant = one(
                c,
                "SELECT id FROM lead.authorization_grants WHERE target_id=%s AND revoked_at IS NULL ORDER BY version DESC LIMIT 1",
                (binding["target_id"],),
            )
            created = probe_in_tx(
                c, aid, target, grant["id"], a["authorization_version"], notify=False
            )
            probe = {"id": created["card_id"], "version": created["version"]}
    accept_card(
        aid,
        str(probe["id"]),
        probe["version"],
        True,
        delegation_id=str(account["delegation_id"]),
        bootstrap_run_id=str(task["id"])
        if task["input"].get("fresh_channel")
        else None,
    )
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.tasks SET state='succeeded',completed_at=now(),result=coalesce(result,'{}') || %s WHERE id=%s",
            (
                j(
                    {
                        "target_id": str(binding["target_id"]),
                        "verification_card_id": str(probe["id"]),
                        **(
                            {
                                "awaiting_first_publish": True,
                                "acceptance_state": "awaiting_verification",
                            }
                            if task["input"].get("fresh_channel")
                            else {}
                        ),
                    }
                ),
                task["id"],
            ),
        )
        record(c, aid, "Telegram 频道已准备，正在验证发布连接", task_id=task["id"])
        emit(c, aid, "supervisor.wake")
    return {"delay": 60, "reason": "account_verifying"}


async def recover_saved_login(aid, rpc):
    """Recheck a known session before asking the owner to log in again.

    Only an unsubmitted verification may be replaced. Unknown/dispatching writes
    keep their existing ledger and reconciliation owner.
    """
    with tx(aid) as c:
        a = one(c, "SELECT * FROM lead.agents WHERE id=%s", (aid,))
        if not a or a["desired_state"] != "running":
            return
        if one(
            c,
            "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now()",
        ) or one(
            c,
            "SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()",
        ):
            return
        account = one(
            c,
            "SELECT s.* FROM lead.social_accounts s WHERE s.platform='telegram' AND s.state='needs_auth' AND s.automation_enabled AND s.external_account_id IS NOT NULL AND EXISTS(SELECT 1 FROM lead.interaction_cards card WHERE card.type='manage_account' AND card.state='accepted' AND card.expires_at>now() AND card.payload->>'account_id'=s.id::text AND card.payload->>'external_account_id'=s.external_account_id) LIMIT 1",
        )
        if not account:
            return
        if one(
            c,
            "SELECT 1 FROM lead.tasks WHERE type='account.check_login' AND input->>'account_id'=%s AND started_at>now()-interval '10 minutes'",
            (str(account["id"]),),
        ):
            return
        check = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at,started_at) VALUES(%s,%s,'account.check_login','running',%s,%s,now(),now()) RETURNING id",
            (
                current_tenant(),
                aid,
                "login-check:" + uid(),
                j(
                    {
                        "account_id": str(account["id"]),
                        "control_version": a["control_version"],
                    }
                ),
            ),
        )
    try:
        observed = await rpc(
            "/agents/" + aid + "/account/inspect", {"platform": "telegram"}
        )
        if observed["external_account_id"] != account["external_account_id"]:
            raise ValueError("登录账号与原授权不一致")
        with tx(aid) as c:
            current = require_agent(c, aid, True)
            if (
                current["desired_state"] != "running"
                or current["control_version"] != a["control_version"]
            ):
                c.execute(
                    "UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE id=%s",
                    (check["id"],),
                )
                return
            known = one(
                c,
                "SELECT * FROM lead.social_accounts WHERE id=%s FOR UPDATE",
                (account["id"],),
            )
            if (
                not known
                or known["external_account_id"] != observed["external_account_id"]
                or not known["automation_enabled"]
            ):
                raise ValueError("账号绑定已变化")
            c.execute(
                "UPDATE lead.social_accounts SET state='connected',verified_at=now() WHERE id=%s",
                (account["id"],),
            )
            for pending in all_rows(
                c,
                "SELECT t.id,x.state AS action_state FROM lead.tasks t LEFT JOIN lead.actions x ON x.task_id=t.id WHERE t.type='connection.verify_publish' AND t.state='waiting_user' AND t.result->>'error'='ACCOUNT_NEEDS_AUTH' AND t.input->>'target_id' IN (SELECT id::text FROM lead.account_targets WHERE account_id=%s)",
                (account["id"],),
            ):
                if pending["action_state"] in ("dispatching", "unknown", "confirmed"):
                    c.execute(
                        "UPDATE lead.tasks SET state='reconciling' WHERE id=%s",
                        (pending["id"],),
                    )
                    emit(
                        c,
                        aid,
                        "task.reconcile",
                        {
                            "task_id": str(pending["id"]),
                            "recovery_id": str(check["id"]),
                        },
                    )
                    continue
                c.execute(
                    "UPDATE lead.actions SET state='canceled' WHERE task_id=%s AND state='prepared'",
                    (pending["id"],),
                )
                c.execute(
                    "UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE id=%s",
                    (pending["id"],),
                )
            c.execute(
                "UPDATE lead.tasks SET state='succeeded',completed_at=now(),result=%s WHERE id=%s",
                (j({"session_verified": True}), check["id"]),
            )
            c.execute(
                "UPDATE lead.agents SET health_state='queued' WHERE id=%s AND desired_state='running'",
                (aid,),
            )
            record(
                c,
                aid,
                "已自动核对保存的 Telegram 登录",
                "会话仍有效，无需重新登录。",
                task_id=check["id"],
            )
            emit(c, aid, "supervisor.wake")
    except Exception as error:
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET state='waiting_user',completed_at=now(),result=%s WHERE id=%s",
                (
                    j({"session_verified": False, "error_type": type(error).__name__}),
                    check["id"],
                ),
            )


def setup_guard(actor, c, task):
    from .config import env

    a = require_agent(c, actor.aid, True)
    account = one(
        c,
        "SELECT * FROM lead.social_accounts WHERE id=%s",
        (task["input"]["account_id"],),
    )
    require_delegation(c, a, account, task["input"]["delegation_id"])
    if (
        task["type"] != "account.prepare"
        or task["state"] != "running"
        or account["external_account_id"] != task["input"]["external_account_id"]
    ):
        raise ValueError("运营筹备任务已失效")
    actor.gate(
        c,
        {
            "intent": {"platform": "telegram"},
            "expected_control_version": task["input"]["control_version"],
            "expected_config_revision": task["input"]["config_revision"],
        },
        account_setup=True,
    )
    if env().get("LEAD_DEPLOYMENT_MODE") == "production" and not one(
        c,
        "SELECT 1 FROM lead.identity_checks i JOIN lead.network_identities n ON n.id=i.network_identity_id JOIN lead.browser_profiles p ON p.id=i.profile_id AND p.config_hash=i.config_hash AND p.engine_version=i.engine_version AND p.network_identity_id=n.id WHERE i.result='passed' AND i.valid_until>now() AND n.valid_until>now() AND n.expected_ip=i.observed_ip AND n.mode='dedicated' AND n.state='verified'",
    ):
        raise ValueError("运营环境核验尚未通过，已停止频道设置")
    return account


async def prepare_in_browser(actor, task_id):
    from . import telegram

    async with actor.lock:
        actor.touch()
        with tx(actor.aid) as c:
            task = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (task_id,))
            if not task:
                raise LookupError("运营筹备任务不存在")
            account = setup_guard(actor, c, task)
            saved = task["result"] or {}
        phase = "open_account"
        try:
            await telegram.open_account(actor.page, account["external_account_id"])
            phase = "reconcile_channel"
            if saved.get("target"):
                observed = await telegram.inspect_target(
                    actor.page,
                    saved["target"]["canonical_url"],
                    account["external_account_id"],
                )
            else:
                observed = await telegram.find_prepared_channel(
                    actor.page, task["input"]
                )
                if not observed:
                    if saved.get("creation_state") in (
                        "dispatching",
                        "unknown",
                        "confirmed",
                    ):
                        diagnostic = await actor.page.evaluate(
                            """setup => ({expectedTitleNodes:[...document.querySelectorAll('.peer-title')].filter(e=>e.textContent.trim()===setup.title).map(e=>({cls:e.className,peer:e.dataset.peerId,parent:e.parentElement?.className,visible:!!e.getBoundingClientRect().width})),form:[...document.querySelectorAll('.new-channel-container .input-field')].map(e=>({error:e.classList.contains('is-error'),label:e.querySelector('.input-field-border')?.textContent,fieldClass:e.querySelector('.input-field-input')?.className})),newFormVisible:[...document.querySelectorAll('.new-channel-container')].some(e=>!!e.getBoundingClientRect().width),membersVisible:[...document.querySelectorAll('.add-members-container')].some(e=>!!e.getBoundingClientRect().width),dialogs:[...document.querySelectorAll('.popup-container')].filter(e=>!!e.getBoundingClientRect().width).map(e=>e.textContent.slice(0,500)),headerMatches:[...document.querySelectorAll('.chat-info .peer-title')].some(e=>e.textContent.trim()===setup.title),hash:location.hash})""",
                            task["input"],
                        )
                        with tx(actor.aid) as c:
                            c.execute(
                                "UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s",
                                (
                                    j(
                                        {
                                            "phase": phase,
                                            "reason": "created_channel_not_observed",
                                            "diagnostic": diagnostic,
                                        }
                                    ),
                                    task_id,
                                ),
                            )
                        return {
                            "state": "unknown",
                            "reason": "正在核对已创建频道，未重复创建",
                        }
                    phase = "create_channel"
                    await telegram.fill_new_channel(actor.page, task["input"])
                    if (await telegram.inspect_account(actor.page))[
                        "external_account_id"
                    ] != account["external_account_id"]:
                        raise telegram.TelegramNeedsAuth(
                            "Telegram 登录账号已变化，已停止创建频道"
                        )
                    # Commit BEFORE the external write. Keep the agent lock across click,
                    # exactly as publishing does, so pause fences subsequent submissions.
                    with tx(actor.aid) as c:
                        setup_guard(actor, c, task)
                        c.execute(
                            "UPDATE lead.tasks SET result=%s WHERE id=%s",
                            (j({"creation_state": "dispatching"}), task_id),
                        )
                    with tx(actor.aid) as c:
                        setup_guard(actor, c, task)
                        await telegram.submit_new_channel(actor.page)
                    phase = "inspect_created_channel"
                    observed = await telegram.created_channel(
                        actor.page, task["input"], account["external_account_id"]
                    )
                with tx(actor.aid) as c:
                    c.execute(
                        "UPDATE lead.tasks SET result=%s WHERE id=%s",
                        (
                            j({"creation_state": "confirmed", "target": observed}),
                            task_id,
                        ),
                    )
            # Only the dedicated channel whose exact title/about were checked is edited.
            with tx(actor.aid) as c:
                setup_guard(actor, c, task)
            phase = "public_settings"
            changed = await telegram.fill_public_channel(actor.page, task["input"])
            if changed:
                if (await telegram.inspect_account(actor.page))[
                    "external_account_id"
                ] != account["external_account_id"]:
                    raise telegram.TelegramNeedsAuth(
                        "Telegram 登录账号已变化，已停止频道设置"
                    )
                with tx(actor.aid) as c:
                    setup_guard(actor, c, task)
                    await telegram.submit_public_channel(actor.page, task["input"])
            phase = "verify_public_link"
            observed = await telegram.inspect_target(
                actor.page,
                "https://t.me/" + task["input"]["handle"],
                account["external_account_id"],
            )
            observed["public_url"] = "https://t.me/" + task["input"]["handle"]
            with tx(actor.aid) as c:
                c.execute(
                    "UPDATE lead.tasks SET result=%s WHERE id=%s",
                    (j({"creation_state": "confirmed", "target": observed}), task_id),
                )
            return {"state": "confirmed", "target": observed}
        except Exception as error:
            try:
                diagnostic = await actor.page.evaluate(
                    """handle => ({tabs:['profile-container','edit-group-container','group-type-container'].flatMap(cls=>[...document.querySelectorAll('.'+cls)].map(e=>({cls:e.className,display:getComputedStyle(e).display,x:Math.round(e.getBoundingClientRect().x),width:Math.round(e.getBoundingClientRect().width)}))),publicRows:[...document.querySelectorAll('.profile-container .row.row-grid .row-title')].map(e=>({matches:e.textContent.trim()==='t.me/'+handle,visible:!!e.getBoundingClientRect().width})),publicChecked:document.querySelector('.group-type-container input[value=public]')?.checked,usernameMatches:document.querySelector('[name=group-public-link]')?.value==='t.me/'+handle})""",
                    task["input"]["handle"],
                )
            except Exception:
                diagnostic = {"page_unavailable": True}
            with tx(actor.aid) as c:
                c.execute(
                    "UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s",
                    (
                        j(
                            {
                                "phase": phase,
                                "error_type": type(error).__name__,
                                "error_detail": str(error)[:1600],
                                "diagnostic": diagnostic,
                            }
                        ),
                        task_id,
                    ),
                )
                current = (
                    one(c, "SELECT result FROM lead.tasks WHERE id=%s", (task_id,))[
                        "result"
                    ]
                    or {}
                )
                uncertain = current.get("creation_state") in ("dispatching", "unknown")
                if uncertain:
                    c.execute(
                        "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
                        (j({"creation_state": "unknown"}), task_id),
                    )
                if isinstance(error, telegram.TelegramNeedsAuth):
                    c.execute(
                        "UPDATE lead.social_accounts SET state='needs_auth' WHERE id=%s AND state='connected'",
                        (account["id"],),
                    )
                if isinstance(
                    error, (telegram.TelegramNeedsAuth, telegram.TelegramSetupBlocked)
                ):
                    c.execute(
                        "UPDATE lead.tasks SET state='waiting_user',result=coalesce(result,'{}') || %s WHERE id=%s",
                        (j({"reason": str(error)}), task_id),
                    )
                    c.execute(
                        "UPDATE lead.agents SET health_state=CASE WHEN desired_state='running' THEN %s ELSE health_state END WHERE id=%s",
                        (
                            "needs_auth"
                            if isinstance(error, telegram.TelegramNeedsAuth)
                            else "needs_attention",
                            actor.aid,
                        ),
                    )
                    record(
                        c,
                        actor.aid,
                        "Telegram 运营筹备暂未完成",
                        str(error),
                        "warning",
                        task_id=task_id,
                    )
                    return {"state": "blocked", "reason": str(error)}
            if uncertain:
                return {"state": "unknown"}
            raise
