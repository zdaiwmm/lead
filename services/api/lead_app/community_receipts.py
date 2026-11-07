"""Read-only recovery of durable Telegram actions on their original account and peer."""

import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout

from . import telegram
from .db import j, tx


async def bound_view(page, account, peer):
    """Resolve the persisted numeric peer without reopening its public profile."""
    await telegram.open_account(page, account)
    current = await page.evaluate(telegram.TARGET)
    if current.get("target_id") != peer:
        await telegram.close_forum_topics(page)
        await telegram.close_chat_search(page)
        route = urlsplit(page.url)._replace(fragment=peer).geturl()
        await page.goto(route, wait_until="domcontentloaded", timeout=20000)
    await telegram.open_forum_as_messages(page, peer)
    header = (await telegram.selected_chat(page)).locator(
        '.chat-info .peer-title[data-peer-id="' + peer + '"]'
    ).filter(visible=True)
    await header.wait_for(timeout=10000)
    identity = await telegram.inspect_account(page)
    observed = await page.evaluate(telegram.TARGET)
    if identity["external_account_id"] != account or observed.get("target_id") != peer:
        raise telegram.TelegramTargetMismatch("回执账号或原群编号无法核对")
    return observed


async def reconcile(actor, task, account, peer, limit):
    from . import community_runtime as runtime

    if not peer or not re.fullmatch(r"-\d+", peer):
        raise ValueError("缺少原动作绑定的群编号，等待恢复原证据")
    saved = task["result"] or {}
    started = time.monotonic()
    timings = {}
    phase = "navigation"
    check = {"at": datetime.now(timezone.utc).isoformat(), "phase": phase,
             "target_id": peer, "status": "reading", "timings_ms": timings}
    evidence = {}
    try:
        try:
            await bound_view(actor.page, account["external_account_id"], peer)
        except BrowserTimeout:
            await runtime.reload_telegram(actor.page, account["external_account_id"])
            await bound_view(actor.page, account["external_account_id"], peer)
        timings["navigation"] = round((time.monotonic() - started) * 1000)
        phase = "message" if saved.get("reply_state") in ("dispatching", "unknown") else "membership"
        read_started = time.monotonic()
        if phase == "message":
            publication = task["input"].get("reply") or {}
            body, baseline = publication.get("body"), saved.get("baseline_mid")
            receipt = await runtime.own_reply_receipt(actor.page, body, baseline) if body and baseline is not None else None
            if not receipt and body and baseline is not None:
                await runtime.search_history(actor.page, " ".join(body.split())[:40], peer, limit)
                if (await actor.page.evaluate(telegram.TARGET))["target_id"] == peer:
                    receipt = await runtime.own_reply_receipt(actor.page, body, baseline)
            if receipt:
                external = peer + ":" + receipt["mid"]
                evidence = {"reply_state": "confirmed", "reply_external_id": external,
                            "reply_confirmed_at": datetime.now(timezone.utc).isoformat()}
                outcome = {"state": "confirmed", "external_id": external}
            else:
                # A failed bubble is a terminal receipt, including after a lost
                # submit response. Never click it to resend.
                diagnostic = await telegram.receipt_diagnostic(actor.page, body, body[:80]) if body and baseline is not None else {}
                failed = diagnostic.get("failedMatching") == 1
                evidence = {"reply_state": "failed" if failed else "unknown", "receipt_diagnostic": diagnostic}
                outcome = {"state": "failed" if failed else "unknown", "reason": "客户端已明确标记原消息发送失败" if failed else "原消息尚未取得回执，已安排再次核对"}
        elif saved.get("join_state") in ("dispatching", "unknown", "pending_approval"):
            controls = await runtime.membership_controls(actor.page)
            state = ("pending_approval" if runtime.pending_join_request(controls) else
                     "confirmed" if runtime.native_membership(controls) else "unknown")
            evidence = {"join_state": state, "join_checked_at": datetime.now(timezone.utc).isoformat()}
            if state == "pending_approval":
                from .community_schedule import approval_review_at
                evidence["approval_review_after"] = approval_review_at(saved, datetime.now(timezone.utc)).isoformat()
            if state == "confirmed" and not saved.get("join_confirmed_at"):
                evidence["join_confirmed_at"] = datetime.now(timezone.utc).isoformat()
            check["membership"] = {key: controls[key] for key in (
                "join_button_count", "apply_button_count", "controls", "editor_metadata"
            )}
            outcome = {"state": state, "reason": {
                "confirmed": "已核对原群成员身份",
                "pending_approval": "平台显示申请待批准，等待群管理员处理",
                "unknown": "平台尚未确认原入群动作，已安排再次核对",
            }[state]}
        elif saved.get("reply_state") == "confirmed" and saved.get("reply_external_id"):
            outcome = {"state": "confirmed", "external_id": saved["reply_external_id"]}
        elif saved.get("join_state") == "confirmed" and not task["input"].get("reply"):
            outcome = {"state": "confirmed"}
        else:
            outcome = {"state": "unknown", "reason": "原动作证据尚不完整"}
        timings[phase] = round((time.monotonic() - read_started) * 1000)
        if (await actor.page.evaluate(telegram.TARGET)).get("target_id") != peer:
            raise telegram.TelegramTargetMismatch("核对期间目标群发生变化")
        check.update(status=outcome["state"], phase=phase)
        return outcome
    except (Exception, asyncio.CancelledError) as error:
        check.update(status="read_failed", phase=phase, error=type(error).__name__,
                     detail=str(error)[:500])
        raise
    finally:
        check["duration_ms"] = round((time.monotonic() - started) * 1000)
        with tx(actor.aid) as c:
            runtime.guard(actor, c, task, reading_receipt=task["state"] == "reconciling")
            if check["status"] == "failed":
                from .community_health import failed
                failed(c, actor.aid, task, {"error_code": "send_failed", "stage": "receipt", "scope": "target", "retryable": True, "write_may_have_happened": False})
            c.execute("UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s",
                      (j({**(evidence if check["status"] != "read_failed" else {}),
                          "receipt_observation": check,
                          "receipt_observations": [*(saved.get("receipt_observations") or [])[-7:], check]}), task["id"]))
