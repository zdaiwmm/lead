"""Telegram Web K adapter. Uses the logged-in website, never Bot/MTProto APIs.

Only user-managed broadcast channels are eligible. It does not harvest third-party
messages or send chat histories to a model; receipt matching stays in the browser.
"""

import asyncio
import re
from urllib.parse import urlencode, urlsplit


class TelegramNeedsAuth(ValueError):
    pass


class TelegramTargetMismatch(ValueError):
    pass


def channel_url(value):
    value = value.strip()
    if value.startswith("@"):
        value = "https://t.me/" + value[1:]
    p = urlsplit(value)
    if p.scheme != "https" or p.username or p.password:
        raise ValueError("请填写 Telegram 频道链接")
    if p.hostname in ("t.me", "telegram.me"):
        handle = p.path.strip("/")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", handle):
            raise ValueError("首版需要自有公开频道的 t.me/频道名链接")
        return "https://web.telegram.org/k/#@" + handle
    if (
        p.hostname == "web.telegram.org"
        and p.path.startswith("/k/")
        and re.fullmatch(r"-?\d+|@[A-Za-z][A-Za-z0-9_]{3,31}", p.fragment)
    ):
        return p.geturl()
    raise ValueError("请填写 Telegram 频道链接")


OWN_ID = r"""() => {try {const slot=new URL(location.href).searchParams.get('account')||'1';if(!/^[1-4]$/.test(slot))return {account_id:null,ready:false};const stored=JSON.parse(localStorage.getItem('account'+slot)||'null');const legacy=slot==='1'?JSON.parse(localStorage.getItem('user_auth')||'null'):null;const id=String(stored?.userId||legacy?.id||'');const visible=el=>!!(el.getBoundingClientRect().width&&el.getBoundingClientRect().height);const ready=Array.from(document.querySelectorAll('.chatlist,.chatlist-container,.chat-list')).some(visible);return {account_id:/^\d+$/.test(id)?id:null,slot,ready}}catch{return {account_id:null,ready:false}}}"""
TARGET = r"""() => {const visible=el=>!!(el.getBoundingClientRect().width&&el.getBoundingClientRect().height);const header=Array.from(document.querySelectorAll('.chat-info')).find(visible);const peer=header?.querySelector('.peer-title[data-peer-id]')||header?.querySelector('[data-peer-id]');const editor=Array.from(document.querySelectorAll('.input-message-input[contenteditable=true]')).find(visible);const subtitle=header?.innerText||'';const placeholder=editor?.getAttribute('data-placeholder')||editor?.getAttribute('aria-label')||editor?.getAttribute('placeholder')||editor?.parentElement?.innerText||'';return {target_id:peer?.getAttribute('data-peer-id')||null,title:peer?.textContent?.trim()||'',header:subtitle.slice(0,300),can_write:!!editor,placeholder:placeholder.slice(0,200),channel_hint:/subscribers?|订阅者|位订阅|频道|broadcast/i.test(subtitle+' '+placeholder)}}"""


async def inspect_account(page):
    p = urlsplit(page.url)
    if p.hostname != "web.telegram.org" or not p.path.startswith("/k/"):
        raise TelegramNeedsAuth("请先在 Telegram 浏览器中完成登录")
    result = await page.evaluate(OWN_ID)
    if not result["account_id"] or not result["ready"]:
        raise TelegramNeedsAuth("Telegram 尚未完成登录或页面尚未就绪")
    return {
        "platform": "telegram",
        "external_account_id": result["account_id"],
        "display_name": "Telegram 账号",
        "slot": result["slot"],
    }


async def inspect_target(page, url, expected_account):
    target_url = channel_url(url)
    before = await inspect_account(page)
    if before["external_account_id"] != expected_account:
        raise TelegramTargetMismatch("登录账号已变化，请重新连接账号")
    # A bare /k/ navigation would silently switch a second account back to slot 1.
    p = urlsplit(target_url)
    target_url = p._replace(query=urlencode({"account": before["slot"]})).geturl()
    # Clear the previous chat DOM before hash navigation can resolve another peer.
    await page.goto("about:blank")
    await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
    # Hash navigation completes before Telegram has resolved the peer.
    await (
        page.locator(".chat-info .peer-title[data-peer-id]")
        .filter(visible=True)
        .wait_for(timeout=20000)
    )
    result = None
    for _ in range(30):
        result = await page.evaluate(TARGET)
        current_fragment = urlsplit(page.url).fragment
        requested = urlsplit(target_url).fragment
        resolved = re.fullmatch(
            r"-\d+", current_fragment
        ) and current_fragment == result.get("target_id")
        if requested.startswith("@"):
            resolved = (
                resolved
                or await page.locator('a[href="https://t.me/' + requested[1:] + '"]')
                .filter(visible=True)
                .count()
                > 0
            )
        if result["can_write"] and result["channel_hint"] and resolved:
            break
        await asyncio.sleep(0.2)
    after = await inspect_account(page)
    if after["external_account_id"] != expected_account:
        raise TelegramTargetMismatch("登录账号已变化，请重新连接账号")
    if (
        not result
        or not re.fullmatch(r"-\d+", result["target_id"] or "")
        or not result["can_write"]
        or not result["channel_hint"]
        or not resolved
    ):
        raise TelegramTargetMismatch(
            "无法确认这是你有发布权限的广播频道，请打开自有频道并核对权限"
        )
    if (
        re.fullmatch(r"-?\d+", urlsplit(target_url).fragment)
        and result["target_id"] != urlsplit(target_url).fragment
    ):
        raise TelegramTargetMismatch("当前频道与授权目标不一致")
    return {
        "external_target_id": result["target_id"],
        "display_name": result["title"],
        "canonical_url": "https://web.telegram.org/k/?"
        + urlencode({"account": after["slot"]})
        + "#"
        + result["target_id"],
        "public_url": url if url.startswith("https://t.me/") else None,
        "verified_permissions": ["publish"],
        "target_type": "channel",
    }


async def baseline(page):
    return await page.evaluate(
        "() => Array.from(document.querySelectorAll('.bubble[data-mid]')).reduce((max,el)=>{try{const id=BigInt(el.dataset.mid);return id>max?id:max}catch{return max}},0n).toString()"
    )


async def fill(page, body):
    if not body.strip() or len(body) > 4096:
        raise ValueError("Telegram 内容长度不符合要求")
    await (
        page.locator(".input-message-input[contenteditable=true]")
        .filter(visible=True)
        .fill(body)
    )


async def submit(page):
    await page.locator(".btn-send").filter(visible=True).click(timeout=10000)


async def receipt(page, body, minimum_mid, marker):
    if not marker or marker not in body:
        raise ValueError("发布缺少可核对的唯一标识")
    # Returns only the matching own content ID, never other messages or chat text.
    return await page.evaluate(
        r"""({body,minimum,marker})=>{const normalize=s=>s.replace(/\s+/g,' ').trim();const matches=Array.from(document.querySelectorAll('.bubble[data-mid]')).filter(el=>{let mid;try{mid=BigInt(el.dataset.mid)}catch{return false}const message=el.querySelector('.message,.message-text');return mid>BigInt(minimum)&&message&&message.innerText.includes(marker)&&normalize(message.innerText).startsWith(normalize(body))&&!el.matches('.is-sending,.sending,.is-error,.error')&&!el.querySelector('.sending,.is-sending,.tgico-sending,.tgico-sendingerror')});return matches.length===1?{mid:matches[0].dataset.mid}:null}""",
        {"body": body, "minimum": str(minimum_mid), "marker": marker},
    )
