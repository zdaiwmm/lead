"""Read-only recovery of durable Telegram actions on their original account and peer."""

import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import telegram
from .db import j, one, tx


async def message_observation(page, peer, body, baseline, prior_failed=None):
    """Match one outgoing message in the verified chat, after the submission baseline."""
    if not body or not str(baseline).isdigit():
        return {"state": "unknown"}
    chat = await telegram.selected_chat(page)
    result = await chat.evaluate(
        r"""(root, {peer, body, baseline, priorFailed}) => {
          const visible = e => !!(e.getBoundingClientRect().width && e.getBoundingClientRect().height);
          const headers = [...root.querySelectorAll('.chat-info .peer-title[data-peer-id]')].filter(visible);
          if (headers.length !== 1 || headers[0].dataset.peerId !== peer) return {state: 'wrong_peer'};
          const norm = text => text.replace(/\s+/g, ' ').trim();
          const matches = [];
          for (const bubble of root.querySelectorAll('.bubble.is-out[data-mid]')) {
            const mid = bubble.dataset.mid;
            if (priorFailed?.includes(mid) || !visible(bubble) || !/^\d+(?:\.\d+)?$/.test(mid)) continue;
            const [whole, fraction] = mid.split('.');
            if (BigInt(whole) < BigInt(baseline) || (BigInt(whole) === BigInt(baseline) && !fraction)) continue;
            const message = bubble.querySelector('.message,.message-text')?.cloneNode(true);
            if (!message) continue;
            message.querySelectorAll('.time,.time-inner,.reactions,.message-reactions,.reply,.webpage').forEach(el => el.remove());
            message.querySelectorAll('br').forEach(el => el.replaceWith(' '));
            message.querySelectorAll('div,p').forEach(el => el.prepend(' '));
            if (norm(message.textContent) !== norm(body)) continue;
            const failed = bubble.matches('.is-error,.error') || !!bubble.querySelector('.tgico-sendingerror');
            // Older submissions did not snapshot temporary IDs already visible in the editor.
            if (failed && priorFailed === null && BigInt(whole) === BigInt(baseline)) continue;
            const pending = bubble.matches('.is-sending,.sending') || !!bubble.querySelector('.sending,.is-sending,.tgico-sending');
            matches.push({state: failed ? 'failed' : pending || fraction ? 'unknown' : 'confirmed', mid});
          }
          // Web K may leave a local sending placeholder beside the acknowledged server bubble.
          const terminal = matches.filter(match => match.state !== 'unknown');
          if (terminal.length === 1 && terminal[0].state === 'confirmed') return terminal[0];
          return matches.length === 1 ? matches[0] : {state:'unknown'};
        }""",
        {"peer": peer, "body": body, "baseline": str(baseline), "priorFailed": prior_failed},
    )
    if result["state"] == "wrong_peer":
        raise telegram.TelegramTargetMismatch("发送回执的当前群与原群不匹配")
    return result


def save_message(c, aid, task, peer, observation):
    """Never let a late read downgrade a terminal receipt or repeat the write."""
    row = one(c, "SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE", (task["id"],))
    saved = row["result"] or {}
    if saved.get("target_id") != peer:
        raise telegram.TelegramTargetMismatch("回执与持久化目标不匹配")
    state = saved.get("reply_state")
    if state == "confirmed" and saved.get("reply_external_id"):
        return {"state": state, "external_id": saved["reply_external_id"]}
    if state == "failed":
        return {"state": state, "reason": "客户端已明确标记原消息发送失败"}
    if state not in ("dispatching", "unknown"):
        raise ValueError("没有已提交的原消息可供核对")
    state = observation["state"]
    evidence = {"reply_state": state}
    outcome = {"state": state}
    if state == "confirmed":
        external = peer + ":" + observation["mid"]
        evidence.update(reply_external_id=external, reply_confirmed_at=datetime.now(timezone.utc).isoformat())
        outcome["external_id"] = external
    elif state == "failed":
        from .community_health import failed
        evidence["reply_failed_at"] = datetime.now(timezone.utc).isoformat()
        evidence["receipt_diagnostic"] = {"failedMatching": 1, "mid": observation["mid"]}
        failed(c, aid, row, {"error_code": "send_failed", "stage": "reply", "scope": "target",
                             "retryable": True, "write_may_have_happened": False})
        outcome["reason"] = "客户端已明确标记原消息发送失败"
    else:
        outcome["reason"] = "只读核对原提交，平台回执尚未确认"
    c.execute("UPDATE lead.tasks SET result=result || %s WHERE id=%s", (j(evidence), task["id"]))
    return outcome


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
        await bound_view(actor.page, account["external_account_id"], peer)
        timings["navigation"] = round((time.monotonic() - started) * 1000)
        phase = "message" if saved.get("reply_state") in ("dispatching", "unknown") else "membership"
        read_started = time.monotonic()
        if phase == "message":
            publication = task["input"].get("reply") or {}
            body, baseline = publication.get("body"), saved.get("baseline_mid")
            observation = await message_observation(actor.page, peer, body, baseline, saved.get("prior_failed_mids"))
            if observation["state"] == "unknown" and body and baseline is not None:
                await runtime.search_history(actor.page, " ".join(body.split())[:40], peer, limit)
                observation = await message_observation(actor.page, peer, body, baseline, saved.get("prior_failed_mids"))
            with tx(actor.aid) as c:
                outcome = save_message(c, actor.aid, task, peer, observation)
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
            c.execute("UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s",
                      (j({**(evidence if check["status"] != "read_failed" else {}),
                          "receipt_observation": check,
                          "receipt_observations": [*(saved.get("receipt_observations") or [])[-7:], check]}), task["id"]))
