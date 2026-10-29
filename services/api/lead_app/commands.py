"""Versioned, idempotent preference commands bound to the actual user message."""

import re
from uuid import UUID

from .config import TENANT
from .db import all_rows, audit, j, one, require_agent
from .domain import remember, snapshot_preferences


def explicit_preference(text, mode):
    if re.search(r"如果|假设|是否|是不是|[?？]", text):
        return False
    if mode in ("replace", "forget"):
        return bool(
            re.search(
                r"忘记|删除|移除|撤销|取消|去掉|不再|改为|改成|替换|纠正|调整", text
            )
        )
    return bool(
        re.search(
            r"记住|以后|今后|后续|默认|偏好|请用|只用|改成|改为|调整|修改|多分享|少发",
            text,
        )
        or re.match(r"\s*(?:请)?(?:别|不要|语气|风格|内容要)", text)
    )


def apply_preference(c, aid, message, decision, expected_revision):
    mode = decision.preference_mode
    if not decision.preference and mode == "add":
        return None
    payload = {
        "mode": mode,
        "ids": decision.preference_ids,
        "text": decision.preference,
    }
    # Invalid IDs fail before entering SQL. Models never choose a different scope.
    ids = list(dict.fromkeys(str(UUID(str(i))) for i in decision.preference_ids))
    previous = one(
        c,
        "SELECT * FROM lead.conversation_commands WHERE message_id=%s AND intent_index=0",
        (message["id"],),
    )
    if previous:
        return (
            previous["result"].get("reply", "此条偏好请求已有记录")
            if previous["result"]
            else "此条偏好请求已有记录"
        )
    command = one(
        c,
        "INSERT INTO lead.conversation_commands(tenant_id,agent_id,conversation_id,message_id,intent_index,command_type,operation_key,expected_revision,payload,state) VALUES(%s,%s,%s,%s,0,'preference.modify',%s,%s,%s,'accepted') RETURNING id",
        (
            TENANT,
            aid,
            message["conversation_id"],
            message["id"],
            str(message["id"]) + ":preference",
            expected_revision,
            j({**payload, "ids": ids}),
        ),
    )
    current = require_agent(c, aid, True)
    if not explicit_preference(message["content"]["text"], mode):
        reply = "运行偏好尚未改变。需要保存或移除偏好时，请直接告诉我。"
        state = "needs_confirmation"
    elif current["config_revision"] != expected_revision:
        reply = "偏好刚有更新，此条修改尚未执行，请按最新偏好再告诉我。"
        state = "failed"
    else:
        if mode in ("replace", "forget"):
            if re.search(r"(?:所有|全部).*偏好", message["content"]["text"]):
                ids = [
                    str(r["id"])
                    for r in all_rows(
                        c,
                        "SELECT id FROM lead.memory_entries WHERE kind='preference' AND deleted_at IS NULL",
                    )
                ]
            if not ids:
                raise ValueError("请明确要移除或替换哪条偏好")
            selected = all_rows(
                c,
                "SELECT id FROM lead.memory_entries WHERE id=ANY(%s::uuid[]) AND kind='preference' AND deleted_at IS NULL",
                (ids,),
            )
            if len(selected) != len(ids):
                raise LookupError("指定偏好不存在或已变化")
            c.execute(
                "UPDATE lead.memory_entries SET deleted_at=now() WHERE id=ANY(%s::uuid[])",
                (ids,),
            )
        if mode != "forget":
            if not decision.preference:
                raise ValueError("缺少新的偏好内容")
            memory_id = remember(c, aid, decision.preference)
            c.execute(
                "UPDATE lead.memory_entries SET source_refs=%s WHERE id=%s",
                (
                    j(
                        [
                            {
                                "message_id": str(message["id"]),
                                "command_id": str(command["id"]),
                            }
                        ]
                    ),
                    memory_id,
                ),
            )
            reply = "已保存运营偏好：" + decision.preference
        else:
            snapshot_preferences(c, aid)
            reply = "已移除指定的运营偏好。"
        state = "succeeded"
        audit(
            c,
            aid,
            "preference.command",
            {"command_id": str(command["id"]), "mode": mode, "ids": ids},
        )
    revision = one(c, "SELECT config_revision FROM lead.agents WHERE id=%s", (aid,))[
        "config_revision"
    ]
    c.execute(
        "UPDATE lead.conversation_commands SET state=%s,result=%s WHERE id=%s",
        (state, j({"reply": reply, "revision": revision}), command["id"]),
    )
    return reply


def apply_asset_annotations(c, aid, message, annotations):
    if not annotations:
        return None
    text = message["content"]["text"]
    explicit = (
        not re.search(r"如果|假设|是否|是不是|[?？]", text)
        and re.search(
            r"AI|人工智能|合成|生成|实拍|自己(?:拍|录)|自行(?:拍|录)", text, re.I
        )
        and (
            message["content"].get("asset_ids")
            or re.search(r"这[张个段份]|刚才|刚上传|上述|素材|视频|图片", text)
        )
    )
    if not explicit:
        return "素材声明尚未改变，请明确说明素材是实拍还是 AI 生成。"
    previous = one(
        c,
        "SELECT result FROM lead.conversation_commands WHERE message_id=%s AND intent_index=1",
        (message["id"],),
    )
    if previous:
        return previous["result"]["reply"]
    a = require_agent(c, aid, True)
    payload = []
    for annotation in annotations:
        asset = one(
            c,
            "SELECT id FROM lead.assets WHERE id=%s AND state='ready'",
            (str(UUID(str(annotation.id))),),
        )
        if not asset:
            raise LookupError("素材不存在")
        c.execute(
            "UPDATE lead.assets SET rights=rights || %s WHERE id=%s",
            (
                j(
                    {
                        "ai_generated": annotation.ai_generated,
                        "declaration_message_id": str(message["id"]),
                    }
                ),
                asset["id"],
            ),
        )
        payload.append(
            {"id": str(asset["id"]), "ai_generated": annotation.ai_generated}
        )
    revision = snapshot_preferences(c, aid)
    reply = "已记录这些素材的 AI 生成声明。"
    c.execute(
        "INSERT INTO lead.conversation_commands(tenant_id,agent_id,conversation_id,message_id,intent_index,command_type,operation_key,expected_revision,payload,result,state) VALUES(%s,%s,%s,%s,1,'asset.annotate',%s,%s,%s,%s,'succeeded')",
        (
            TENANT,
            aid,
            message["conversation_id"],
            message["id"],
            str(message["id"]) + ":assets",
            a["config_revision"],
            j(payload),
            j({"revision": revision, "reply": reply}),
        ),
    )
    audit(
        c,
        aid,
        "asset.declaration",
        {"message_id": str(message["id"]), "assets": payload},
    )
    return reply
