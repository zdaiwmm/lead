"""Check the native link entity and edit only the receipted Telegram text message."""

import re

from . import telegram
from .community_runtime import focus_message
from .db import j, one, record, tx
from .promotion import telegram_link_line


async def link_matches(bubble, link):
    return await bubble.evaluate("""(bubble, link) => {
      const msg = bubble.querySelector('.message,.message-text');
      if (!msg) return false;
      const anchors = [...msg.querySelectorAll('a')].filter(a =>
        a.textContent.includes(link) && !a.closest('.webpage,.webpage-preview'));
      return anchors.length === 1 && anchors[0].href === link &&
        anchors[0].textContent.trim() === link &&
        msg.innerText.split(/\\r?\\n/).some(line => line.trim() === link);
    }""", link)


async def check_action(actor, action_id):
    async with actor.lock:
        actor.touch()
        with tx(actor.aid) as c:
            action = one(c, "SELECT * FROM lead.actions WHERE id=%s AND state='confirmed'", (action_id,))
            if not action:
                raise ValueError("没有已确认的原帖回执")
            intent = action["intent"]
            link = intent.get("link")
            if intent.get("platform") != "telegram" or intent.get("media") or not link:
                return "skipped"
            if not intent.get("marker") or not link.endswith("/r/" + intent["marker"]) or intent["body"].count(link) != 1:
                raise ValueError("原帖缺少唯一推广链接")
            target = one(c, """SELECT t.*,s.external_account_id FROM lead.account_targets t
                JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s""", (action["target_id"],))
            peer, _, mid = (action["external_id"] or "").partition(":")
            if not target or peer != target["external_target_id"] or not mid.isdigit():
                raise ValueError("原帖回执与目标频道不一致")
            actor.gate(c, action)
        page = actor.page
        await telegram.open_account(page, target["external_account_id"])
        await telegram.inspect_target(page, target["canonical_url"], target["external_account_id"])
        await focus_message(page, peer, mid)
        chat = await telegram.selected_chat(page)
        bubble = chat.locator(f'.bubble[data-mid="{mid}"]').filter(visible=True)
        if await link_matches(bubble, link):
            return "verified"

        original = intent["body"]
        corrected = telegram_link_line(original, link)
        editor = chat.locator('.input-message-input[contenteditable=true]').filter(visible=True)
        save = chat.locator(".btn-send.edit").filter(visible=True)
        if await save.count() or (await editor.inner_text()).strip():
            raise ValueError("编辑框中已有草稿，已保留")
        await bubble.locator(".message,.message-text").first.click(button="right")
        edit = page.locator(".btn-menu-item").filter(has=page.get_by_text(re.compile(r"^(Edit|编辑|編輯)$", re.I)), visible=True)
        await edit.click(timeout=5000)
        try:
            await save.wait_for(timeout=5000)
            if " ".join((await editor.inner_text()).split()) != " ".join(original.split()):
                raise ValueError("原帖正文已有其他修改，已保留")
            await editor.fill(corrected)
            if " ".join((await editor.inner_text()).split()) != " ".join(corrected.split()):
                raise ValueError("编辑后的正文不一致")
            if (await telegram.inspect_account(page))["external_account_id"] != target["external_account_id"] or (await page.evaluate(telegram.TARGET))["target_id"] != peer:
                raise ValueError("保存前账号或频道发生变化")
            with tx(actor.aid) as c:
                actor.gate(c, action)
                marked = one(c, """UPDATE lead.action_attempts SET observation=coalesce(observation,'{}') || %s
                    WHERE id=(SELECT id FROM lead.action_attempts WHERE action_id=%s AND outcome='confirmed'
                    ORDER BY attempt DESC LIMIT 1) AND NOT (coalesce(observation,'{}') ? 'link_edit_mid') RETURNING id""",
                    (j({"link_edit_mid": mid}), action_id))
                if not marked:
                    raise ValueError("原帖已尝试修正，等待核对")
            with tx(actor.aid) as c:
                actor.gate(c, action)
                await save.click(timeout=5000)
            await save.wait_for(state="hidden", timeout=10000)
            import asyncio
            for _ in range(20):
                if await link_matches(bubble, link):
                    with tx(actor.aid) as c:
                        record(c, actor.aid, "已修正原帖的推广链接", "原消息已保存，链接单独成行。", task_id=action["task_id"])
                    return "repaired"
                await asyncio.sleep(0.5)
            raise ValueError("已请求保存，原帖链接仍待核对")
        finally:
            if await save.count():
                await editor.press("Escape")
