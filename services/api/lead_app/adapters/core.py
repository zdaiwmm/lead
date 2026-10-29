"""Deterministic browser primitives, with no arbitrary selector/model endpoint."""

import asyncio
import re
from urllib.parse import urlsplit


class NeedsAuth(ValueError):
    pass


class ScopeChanged(ValueError):
    pass


class LayoutChanged(ValueError):
    pass


async def first_visible(page, selectors, timeout=12000):
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while True:
        for selector in selectors:
            items = page.locator(selector).filter(visible=True)
            count = await items.count()
            if count == 1:
                return items
            if count > 1:
                raise LayoutChanged("页面出现多个相同操作目标，已停止")
        if asyncio.get_running_loop().time() >= deadline:
            raise LayoutChanged("页面结构或权限已变化，未找到唯一操作目标")
        await asyncio.sleep(0.15)


async def click(page, selectors):
    await (await first_visible(page, selectors)).click(timeout=12000)


async def fill(page, selectors, text):
    await (await first_visible(page, selectors)).fill(text, timeout=12000)


async def verify_fields(page, fields):
    for field in fields:
        node = await first_visible(page, field["selectors"], timeout=1500)
        value = (
            await node.input_value()
            if await node.evaluate("el => ['INPUT','TEXTAREA'].includes(el.tagName)")
            else await node.inner_text()
        )
        if " ".join(value.split()) != " ".join(field["value"].split()):
            raise ScopeChanged("提交前内容发生变化，已停止")


async def optional_click(page, selectors):
    for selector in selectors:
        item = page.locator(selector).filter(visible=True)
        if await item.count() == 1:
            await item.click(timeout=10000)
            return True
    return False


async def local_account_link(page, selectors):
    found = set()
    for selector in selectors:
        for item in await page.locator(selector).filter(visible=True).all():
            href = await item.get_attribute("href")
            if href:
                found.add(href.split("?")[0].rstrip("/"))
    if len(found) != 1:
        raise NeedsAuth("无法唯一核对当前登录账号，请在专属浏览器中完成登录")
    return next(iter(found))


async def upload_files(page, paths, selectors=("input[type=file]",)):
    candidates = []
    for selector in selectors:
        for node in await page.locator(selector).all():
            if node not in candidates:
                candidates.append(node)
        if candidates:
            break
    if len(candidates) != 1:
        if not candidates:
            async with page.expect_file_chooser(timeout=12000) as chooser:
                await click(
                    page,
                    (
                        'button:text-is("Select from computer")',
                        'button:text-is("Upload from computer")',
                        'button:text-is("Select videos")',
                        '[role=button]:text-is("Select from computer")',
                    ),
                )
            await (await chooser.value).set_files([str(p) for p in paths])
            return
        raise LayoutChanged("无法唯一确认媒体上传入口")
    await candidates[0].set_input_files([str(p) for p in paths], timeout=30000)


async def wait_ready(page):
    # All known busy markers must disappear; the adapter then verifies its final
    # control is enabled. A toast alone can never confirm external publication.
    markers = "[role=progressbar], [aria-busy=true], [data-testid=uploadProgress]"
    deadline = asyncio.get_running_loop().time() + 60
    while await page.locator(markers).filter(visible=True).count():
        if asyncio.get_running_loop().time() > deadline:
            raise LayoutChanged("媒体仍在处理，未进入发布阶段")
        await asyncio.sleep(0.25)


async def check_surface(page, hosts):
    p = urlsplit(page.url)
    if p.scheme != "https" or p.hostname not in hosts:
        raise NeedsAuth("当前不在已验证的平台页面")
    if re.search(
        r"/(login|signin|challenge|checkpoint|authwall)(?:[/?#]|$)", p.path, re.I
    ):
        raise NeedsAuth("需要完成账号登录或验证")
    blocked = 'input[type=password],iframe[src*="captcha"],iframe[title*="challenge"], [data-testid="ocfEnterTextTextInput"]'
    if await page.locator(blocked).filter(visible=True).count():
        raise NeedsAuth("需要在浏览器中完成登录或身份验证")


async def scoped_receipt(
    page,
    containers,
    link_pattern,
    publication,
    account_id,
    author_pattern=None,
    author_selectors="header a[href]",
):
    # Matching is kept in the browser. Only a known own post's URL/ID leaves it.
    return await page.evaluate(
        r"""({containers,pattern,body,marker,owner,authorPattern,authorSelectors})=>{
      const normal=s=>(s||'').replace(/\s+/g,' ').trim();const results=[];
      for(const root of document.querySelectorAll(containers)){
        if(root.matches('[aria-busy=true],.is-sending,.sending,.is-error,.error'))continue;
        const text=normal(root.innerText);if(!marker||!text.includes(marker)||!text.includes(normal(body)))continue;
        const links=Array.from(root.querySelectorAll('a[href]')).map(a=>a.href);
        const authors=Array.from(root.querySelectorAll(authorSelectors)).map(a=>a.href);
        if(authorPattern&&root.getAttribute('author')!==owner&&!authors.some(h=>{if(!new RegExp(authorPattern).test(h))return false;const u=new URL(h);const parts=u.pathname.split('/').filter(Boolean).map(s=>s.replace(/^@/,''));return parts.some(s=>s.toLowerCase()===owner.toLowerCase())||u.searchParams.get('id')===owner;}))continue;
        const href=links.find(h=>new RegExp(pattern).test(h));if(href)results.push(href);
      }const unique=[...new Set(results)];return unique.length===1?{url:unique[0],external_id:unique[0]}:null;
    }""",
        {
            "containers": containers,
            "pattern": link_pattern,
            "body": publication["body"],
            "marker": publication["marker"],
            "owner": account_id,
            "authorPattern": author_pattern,
            "authorSelectors": author_selectors,
        },
    )
