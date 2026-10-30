"""Native account search exports public group metadata, never people/messages."""

import asyncio
import hashlib
import re

from .community_growth import GrowthConfig
from .config import STATE
from .db import j, one, require_agent, tx, uid
from .research import begin_run, finish_run
from .scope import current_tenant

QUERIES = (
    "zotero",
    "siyuan",
    "sioyek",
    "koodo",
    "readlang",
    "pdfmathtranslate",
    "koreader",
    "librera",
    "readera",
    "readest",
    "calibre",
    "readwise",
    "obsidian",
    "deepl",
    "immersive",
    "kindle",
    "epub",
    "沉浸式翻译",
    "阅读器",
    "术语",
)


def guard(actor, c, task):
    from .admin_controls import enforce_channel
    enforce_channel(c, actor.aid, "telegram", operation="community")
    a = require_agent(c, actor.aid, True)
    config = GrowthConfig.model_validate(
        a["onboarding_state"].get("community_growth", {})
    )
    p = task["input"]
    account = one(
        c, "SELECT * FROM lead.social_accounts WHERE id=%s", (p["account_id"],)
    )
    card = one(
        c,
        "SELECT * FROM lead.interaction_cards WHERE id=%s AND type='manage_community' AND state='accepted' AND expires_at>now()",
        (p["delegation_id"],),
    )
    if (
        a["desired_state"] != "running"
        or not config.enabled
        or config.mode != "contextual_outreach"
        or task["type"] != "community.discover"
        or task["state"] != "running"
        or not account
        or account["state"] != "connected"
        or not account["automation_enabled"]
        or account["external_account_id"] != p["external_account_id"]
        or not card
        or card["payload"].get("external_account_id") != account["external_account_id"]
        or card["payload"].get("account_id") != str(account["id"])
        or card["payload"].get("source") != "owner_strategy"
    ):
        raise ValueError("找群任务的账号或委托已变化")
    actor.gate(
        c,
        {
            "intent": {"platform": "telegram"},
            "expected_control_version": p["control_version"],
            "expected_config_revision": p["config_revision"],
        },
        account_setup=True,
    )
    return account


async def native_search(actor, task_id, query):
    from . import telegram

    if not isinstance(query, str) or len(query) > 60:
        raise ValueError("找群查询长度不可用")
    async with actor.lock:
        actor.touch()
        with tx(actor.aid) as c:
            task = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (task_id,))
            if not task:
                raise LookupError("找群任务不存在")
            account = guard(actor, c, task)
            if query not in task["input"].get("native_queries", QUERIES):
                raise ValueError("找群查询不在已保存的 Agent 计划中")
        await telegram.open_account(actor.page, account["external_account_id"])
        if not getattr(actor, "community_search_error_listener", False):
            actor.community_search_errors = []
            actor.page.on(
                "pageerror",
                lambda error: (
                    actor.community_search_errors.append(str(error)[:600])
                    if len(actor.community_search_errors) < 5
                    else None
                ),
            )
            actor.community_search_error_listener = True
        await actor.page.bring_to_front()
        await telegram.close_forum_topics(actor.page)
        await telegram.close_chat_search(actor.page)
        folder = STATE / "evidence" / actor.aid
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        snapshot = folder / (
            "community-search-"
            + str(task_id)
            + "-"
            + hashlib.sha256(query.encode()).hexdigest()[:8]
            + ".png"
        )
        search = actor.page.locator("#column-left .input-search-input").filter(
            visible=True
        )
        await search.first.wait_for(timeout=12000)
        if await search.count() != 1:
            raise ValueError(
                f"原生全局搜索入口匹配 {await search.count()} 个，无法唯一确认"
            )
        await search.click(timeout=10000)
        try:
            await (
                actor.page.locator("#search-container .search-super")
                .filter(visible=True)
                .wait_for(timeout=12000)
            )
        except Exception:
            await actor.page.locator("#column-left").screenshot(path=str(snapshot))
            snapshot.chmod(0o600)
            diagnostic = await actor.page.evaluate(
                """() => ({focused:document.hasFocus(),containers:document.querySelectorAll('#search-container').length,panels:document.querySelectorAll('.search-super').length})"""
            )
            raise ValueError(
                f"原生搜索面板未就绪：{diagnostic}；页面错误：{actor.community_search_errors}；证据：{snapshot}"
            ) from None
        clear = actor.page.locator("#column-left .input-search-clear").filter(
            visible=True
        )
        if await clear.count() == 1:
            await clear.click(timeout=5000)
            await asyncio.sleep(0.2)
        if (
            await actor.page.locator("#column-left .input-search [data-key]")
            .filter(visible=True)
            .count()
        ):
            raise ValueError("全局搜索仍有本群范围标记，未执行跨群查询")
        # Clearing an old query can also close Telegram's search tab while the
        # input retains focus. Re-enter native search before typing; a cached
        # search-super element alone does not prove the active tab is search.
        await search.blur()
        await search.click(timeout=10000)
        await (
            actor.page.locator("#column-left .item-main.is-search-active")
            .filter(visible=True)
            .wait_for(timeout=12000)
        )
        await (
            actor.page.locator("#search-container .search-super")
            .filter(visible=True)
            .wait_for(timeout=12000)
        )
        await search.fill("")
        await asyncio.sleep(0.35)
        await search.fill(query)
        previous = None
        rows = []
        reentered = False
        expansions = 0
        reactivated = False
        for _ in range(24):
            await asyncio.sleep(0.5)
            if (
                not await actor.page.locator("#column-left .item-main.is-search-active")
                .filter(visible=True)
                .count()
                and not reactivated
            ):
                await search.blur()
                await search.click(timeout=10000)
                await (
                    actor.page.locator("#column-left .item-main.is-search-active")
                    .filter(visible=True)
                    .wait_for(timeout=12000)
                )
                await search.fill(query)
                reactivated = True
                previous = None
                continue
            if await search.input_value() != query and not reentered:
                await search.fill(query)
                reentered = True
                previous = None
                continue
            more = (
                actor.page.locator(
                    "#search-container .search-group-contacts .cursor-pointer.hover-underline"
                )
                .filter(has_text=re.compile(r"^(Show more|显示更多|顯示更多)$", re.I))
                .filter(visible=True)
            )
            if expansions < 3 and await more.count():
                await more.first.click(timeout=5000)
                expansions += 1
                previous = None
                continue
            rows = await actor.page.evaluate(
                r"""() => {const found=new Map();for(const el of document.querySelectorAll('.search-group-contacts [data-peer-id]')){const id=el.dataset.peerId;if(Number(id)>=0||!el.getBoundingClientRect().width||el.closest('[data-sponsored=true]'))continue;let card=el;for(let i=0;i<4&&card;i++,card=card.parentElement){if(card.matches('[class*=search-group]'))break;const ids=new Set([...card.querySelectorAll('[data-peer-id]')].map(e=>e.dataset.peerId));if([...ids].some(v=>Number(v)<0&&v!==id))break;const username=card.innerText.match(/@([A-Za-z][A-Za-z0-9_]{3,31})/);if(username){const name=(card.querySelector('.peer-title')?.textContent||(el.matches('.peer-title')?el.textContent:'')).trim();if(name)found.set(id,{peer_id:id,name,telegram_url:'https://t.me/'+username[1],subtitle:card.innerText.replace(name,'').trim().slice(0,180)});break}}}return [...found.values()].slice(0,12)}"""
            )
            signature = [(r["peer_id"], r["telegram_url"]) for r in rows]
            if rows and previous == signature:
                break
            previous = signature
        if not rows:
            await actor.page.locator("#column-left").screenshot(path=str(snapshot))
            snapshot.chmod(0o600)
        if await search.input_value() != query:
            raise ValueError(f"全局搜索查询已变化：{(await search.input_value())[:60]}")
        if (
            not await actor.page.locator("#column-left .item-main.is-search-active")
            .filter(visible=True)
            .count()
        ):
            raise ValueError("原生搜索标签已关闭，未将聊天列表当成空搜索结果")
        with tx(actor.aid) as c:
            guard(actor, c, task)
        rid = begin_run(
            actor.aid,
            "原生全局群搜索：" + query,
            task_id=str(task_id),
            mode="community_native_discovery",
        )
        step = uid()
        output = {
            "url": actor.page.url,
            "title": "Telegram 全局群搜索：" + query,
            "query": query,
            "text": "\n".join(r["name"] + " " + r["subtitle"] for r in rows),
            "elements": [{"href": r["telegram_url"], "name": r["name"]} for r in rows],
            "groups": rows,
            "metadata_only": True,
            "diagnostic_snapshot": str(snapshot) if snapshot.exists() else None,
            "diagnostic": await actor.page.evaluate(
                """() => ({containers:[...document.querySelectorAll('[class*=search]')].filter(e=>e.getBoundingClientRect().width).map(e=>e.className).slice(0,20),globalNegativeNodes:[...document.querySelectorAll('.search-group-contacts [data-peer-id]')].filter(e=>Number(e.dataset.peerId)<0).map(e=>({cls:e.className,tag:e.tagName,parent:e.parentElement?.className})).slice(0,20)})"""
            ),
        }
        with tx(actor.aid) as c:
            c.execute(
                "INSERT INTO lead.browser_steps(id,tenant_id,agent_id,run_id,seq,command_id,operation,input,output,state,ended_at) VALUES(%s,%s,%s,%s,1,%s,'native_group_search',%s,%s,'completed',now())",
                (
                    step,
                    current_tenant(),
                    actor.aid,
                    rid,
                    uid(),
                    j({"query": query}),
                    j(output),
                ),
            )
        finish_run(actor.aid, rid)
        return {
            "groups": rows,
            "diagnostic": output["diagnostic"],
            "source": {
                "url": output["url"],
                "title": output["title"],
                "run_id": rid,
                "evidence_id": step,
                "native": True,
            },
        }
