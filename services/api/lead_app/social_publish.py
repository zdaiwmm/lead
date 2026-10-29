"""One durable submission protocol for every website/media adapter."""

import asyncio

from .adapters import get_adapter
from .adapters.core import NeedsAuth, ScopeChanged
from .assets import resolve
from .config import STATE, TENANT
from .db import j, one, tx, uid


async def execute(actor, action_id):
    aid = actor.aid
    async with actor.lock:
        actor.touch()
        with tx(aid) as c:
            action = one(c, "SELECT * FROM lead.actions WHERE id=%s", (action_id,))
            if not action:
                raise LookupError("发布动作不存在")
            if action["state"] == "confirmed":
                return {"state": "confirmed", "external_id": action["external_id"]}
            if action["state"] in ("canceled", "not_applied"):
                return {"state": action["state"]}
            target = one(
                c,
                "SELECT t.*,s.external_account_id,s.platform FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s",
                (action["target_id"],),
            )
            if not target:
                raise ScopeChanged("授权目标不存在")
            preparation = one(
                c,
                "SELECT * FROM lead.publish_preparations WHERE action_id=%s",
                (action_id,),
            )
            uncertain = action["state"] in ("unknown", "dispatching")
            if one(
                c,
                "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('requested','active','ending') AND expires_at>now()",
            ):
                return {
                    "state": "unknown" if uncertain else "prepared",
                    "reason": "用户正在接管",
                }
            if not uncertain:
                actor.gate(c, action)
            else:
                if action["state"] == "dispatching":
                    c.execute(
                        "UPDATE lead.actions SET state='unknown' WHERE id=%s",
                        (action_id,),
                    )
        adapter = get_adapter(target["platform"])
        publication = action["intent"]
        media_manifest = publication.get("media", [])
        observed = None
        try:
            if uncertain:
                if not preparation or not preparation["observation"]:
                    return {
                        "state": "unknown",
                        "reason": "缺少准备记录，只能人工核对，禁止重发",
                    }
                observed = await adapter.reconcile(
                    actor.page,
                    target,
                    publication,
                    preparation["observation"],
                    target["external_account_id"],
                )
                if not observed:
                    return {
                        "state": "unknown",
                        "reason": "未找到唯一已发布内容，未重新上传或提交",
                    }
            else:
                if preparation and (
                    preparation["stage"] != "ready"
                    or preparation["observation"].get("session_id") != actor.session
                ):
                    return {
                        "state": "blocked",
                        "reason": "媒体准备曾中断，需要核对原草稿，未重新上传",
                    }
                if preparation:
                    observation = preparation["observation"]
                else:
                    media = []
                    paths = []
                    for item in media_manifest:
                        asset, path = resolve(aid, item["id"], item["sha256"])
                        if asset["rights"].get("ai_generated") != item.get(
                            "ai_generated"
                        ):
                            raise ValueError("素材声明已变化，请重新生成待发布内容")
                        media.append(asset)
                        paths.append(path)
                    with tx(aid) as c:
                        actor.gate(c, action)
                        c.execute(
                            "INSERT INTO lead.publish_preparations(tenant_id,agent_id,action_id,stage,manifest) VALUES(%s,%s,%s,'preparing',%s)",
                            (TENANT, aid, action_id, j(media_manifest)),
                        )
                    try:
                        observation = await adapter.prepare(
                            actor.page, target, publication, media, paths
                        )
                        observation["session_id"] = actor.session
                    except BaseException:
                        with tx(aid) as c:
                            c.execute(
                                "UPDATE lead.publish_preparations SET stage='uncertain',updated_at=now() WHERE action_id=%s",
                                (action_id,),
                            )
                        raise
                    with tx(aid) as c:
                        c.execute(
                            "UPDATE lead.publish_preparations SET stage='ready',observation=%s,updated_at=now() WHERE action_id=%s",
                            (j(observation), action_id),
                        )
                await adapter.before_commit(
                    actor.page, target, observation, target["external_account_id"]
                )
                # Check all bytes again after potentially long media processing.
                for item in media_manifest:
                    row, _ = resolve(aid, item["id"], item["sha256"])
                    if row["rights"].get("ai_generated") != item.get("ai_generated"):
                        raise ValueError("媒体声明已变化")
                with tx(aid) as c:
                    actor.gate(c, action)
                    c.execute(
                        "UPDATE lead.actions SET state='dispatching' WHERE id=%s AND state='prepared'",
                        (action_id,),
                    )
                    attempt = one(
                        c,
                        "INSERT INTO lead.action_attempts(tenant_id,agent_id,action_id,browser_session_id,attempt,epoch,control_version,observed_account_id,observed_target_id,observation) SELECT %s,%s,%s,%s,coalesce(max(attempt),0)+1,%s,%s,%s,%s,%s FROM lead.action_attempts WHERE action_id=%s RETURNING id",
                        (
                            TENANT,
                            aid,
                            action_id,
                            actor.session,
                            actor.epoch,
                            action["expected_control_version"],
                            target["external_account_id"],
                            target["external_target_id"],
                            j(observation),
                            action_id,
                        ),
                    )
                try:
                    with tx(aid) as c:
                        actor.gate(c, action)
                        await adapter.submit(actor.page, observation)
                        c.execute(
                            "UPDATE lead.publish_preparations SET stage='submitted',updated_at=now() WHERE action_id=%s",
                            (action_id,),
                        )
                    for _ in range(40):
                        observed = await adapter.receipt(
                            actor.page,
                            target,
                            publication,
                            observation,
                            target["external_account_id"],
                        )
                        if observed:
                            break
                        await asyncio.sleep(0.5)
                    if not observed:
                        raise ValueError("未确认发布结果")
                except Exception:
                    with tx(aid) as c:
                        c.execute(
                            "UPDATE lead.actions SET state='unknown' WHERE id=%s AND state='dispatching'",
                            (action_id,),
                        )
                        c.execute(
                            "UPDATE lead.action_attempts SET outcome='unknown',error_code='ACK_UNCERTAIN',ended_at=now() WHERE id=%s",
                            (attempt["id"],),
                        )
                    return {
                        "state": "unknown",
                        "reason": "提交后的回执不确定，只核对，不重复上传或点击",
                    }
        except (NeedsAuth, ScopeChanged) as error:
            with tx(aid) as c:
                c.execute(
                    "UPDATE lead.social_accounts SET state='needs_auth' WHERE id=%s",
                    (target["account_id"],),
                )
            return {
                "state": "unknown" if uncertain else "needs_auth",
                "error": "ACCOUNT_NEEDS_AUTH",
                "reason": str(error),
            }
        folder = STATE / "evidence" / aid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (uid() + ".png")
        await actor.page.screenshot(path=str(path))
        path.chmod(0o600)
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.actions SET state='confirmed',external_id=%s,external_url=%s,evidence_ref=%s,confirmed_at=now() WHERE id=%s AND state IN ('dispatching','unknown')",
                (observed["external_id"], observed.get("url"), str(path), action_id),
            )
            c.execute(
                "UPDATE lead.action_attempts SET outcome='confirmed',evidence_ref=%s,ended_at=now() WHERE action_id=%s AND attempt=(SELECT max(attempt) FROM lead.action_attempts WHERE action_id=%s)",
                (str(path), action_id, action_id),
            )
        return {
            "state": "confirmed",
            "external_id": observed["external_id"],
            "external_url": observed.get("url"),
            "reconciled": uncertain,
        }


def gate(actor, c, action):
    from .adapters.catalog import SPECS

    target = one(
        c,
        "SELECT t.*,s.state AS account_state,s.platform FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s",
        (action["target_id"],),
    )
    platform = action["intent"]["platform"]
    spec = SPECS[platform]
    from .config import env

    if env().get("LEAD_DEPLOYMENT_MODE") == "production" and not one(
        c,
        "SELECT 1 FROM lead.identity_checks i JOIN lead.network_identities n ON n.id=i.network_identity_id JOIN lead.browser_profiles p ON p.id=i.profile_id AND p.config_hash=i.config_hash AND p.engine_version=i.engine_version AND p.network_identity_id=n.id WHERE i.result='passed' AND i.valid_until>now() AND n.valid_until>now() AND n.expected_ip=i.observed_ip AND n.mode='dedicated' AND n.state='verified'",
    ):
        raise ScopeChanged("独立出口与设备验证尚未通过")
    if (
        not target
        or target["platform"] != platform
        or target["state"] != "active"
        or target["account_state"] != "connected"
    ):
        raise ScopeChanged("账号或目标已失效")
    cap = one(
        c,
        "SELECT * FROM lead.platform_capabilities WHERE platform=%s AND action_type='publish' AND target_type=%s",
        (platform, target["target_type"]),
    )
    if not cap or cap["permission_state"] != "approved":
        raise ScopeChanged("此渠道尚未完成适用权限审查")
    task = one(c, "SELECT input FROM lead.tasks WHERE id=%s", (action["task_id"],))
    grant = one(
        c,
        "SELECT * FROM lead.authorization_grants WHERE id=%s AND revoked_at IS NULL",
        (action["grant_id"],),
    )
    if action["intent"].get("media") and "upload_media" not in grant["allowed_actions"]:
        raise ScopeChanged("授权范围不包含媒体上传")
    kind = action["intent"].get("format", "text")
    if task["input"].get("bootstrap_verification"):
        card = one(
            c,
            "SELECT * FROM lead.interaction_cards WHERE id=%s AND type='verify_publish' AND state='accepted'",
            (task["input"]["key"],),
        )
        if (
            not card
            or card["payload"]["body"] != action["intent"]["body"]
            or card["payload"]["target_id"] != str(action["target_id"])
            or card["payload"]["grant_id"] != str(action["grant_id"])
            or card["payload"].get("media", []) != action["intent"].get("media", [])
        ):
            raise ScopeChanged("验证卡与完整内容不一致")
    else:
        from .db import require_agent

        a = require_agent(c, actor.aid)
        if (
            a["desired_state"] != "running"
            or cap["technical_state"] != "verified"
            or cap["adapter_version"] != spec.version
            or kind
            not in target["verified_permissions"].get("publishing_verified_formats", [])
        ):
            raise ScopeChanged("此目标与发布格式尚未完成真实验证")
    limit = int(grant["limits"].get("publish_per_day", 3))
    used = one(
        c,
        "SELECT count(*) AS n FROM lead.actions WHERE target_id=%s AND id<>%s AND state IN ('dispatching','unknown','confirmed') AND created_at>now()-interval '24 hours'",
        (action["target_id"], action["id"]),
    )["n"]
    if used >= limit:
        raise ValueError("此目标已达到当前发布上限")
