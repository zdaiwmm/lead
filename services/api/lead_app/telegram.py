"""Telegram Web K adapter. Uses the logged-in website, never Bot/MTProto APIs.

Only user-managed broadcast channels are eligible. It does not harvest third-party
messages or send chat histories to a model; receipt matching stays in the browser.
"""

import asyncio
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout


class TelegramNeedsAuth(ValueError):
    pass


class TelegramTargetMismatch(ValueError):
    pass


class TelegramSetupBlocked(ValueError):
    pass


EDIT_BUTTON = '.btn-icon[aria-label="Edit"],.btn-icon[aria-label="编辑"],.btn-icon[aria-label="編輯"],.btn-icon:has(.tgico-edit),.btn-icon.tgico-edit'
FORUM_PANELS = ".topics-container.is-visible,.topics-container.active:not(.is-floating)"


async def dismiss_intro(page):
    tips = page.locator(
        'button[aria-label="Tips"][aria-expanded="true"],button[aria-label="使用提示"][aria-expanded="true"]'
    ).filter(visible=True)
    if await tips.count():
        await tips.click(timeout=10000)


async def open_account(page, expected_account):
    """Select a known login slot without exposing dialogs or message contents."""
    p = urlsplit(page.url)
    if p.hostname != "web.telegram.org" or not p.path.startswith("/k/"):
        await page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded")
    slot = await page.evaluate(
        r"""expected => {
      for(let i=1;i<=4;i++) {try {
        const a=JSON.parse(localStorage.getItem('account'+i)||'null');
        const old=i===1?JSON.parse(localStorage.getItem('user_auth')||'null'):null;
        if(String(a?.userId||old?.id||'')===expected)return String(i);
      } catch {}}
      return null;
    }""",
        expected_account,
    )
    if not slot:
        raise TelegramNeedsAuth("Telegram 登录已失效，请重新连接账号")
    if parse_qs(urlsplit(page.url).query).get("account", ["1"])[0] != slot:
        await page.goto(
            "https://web.telegram.org/k/?account=" + slot,
            wait_until="domcontentloaded",
            timeout=35000,
        )
    await (
        page.locator(".chatlist,.chatlist-container")
        .filter(visible=True)
        .first.wait_for(timeout=25000)
    )
    # Web K can display its first-run carousel after login or a page reload.
    # Only close this named onboarding control, never arbitrary dialogs.
    await dismiss_intro(page)
    own = await inspect_account(page)
    if own["external_account_id"] != expected_account:
        raise TelegramNeedsAuth("Telegram 登录账号已变化，请重新连接账号")


async def selected_chat(page):
    active = page.locator('.chat.active[data-type="chat"]').filter(visible=True)
    if await active.count() == 1:
        return active
    if not await page.locator('.chat[data-type="chat"]').count():
        return page.locator("body")  # legacy DOM / isolated adapter fixtures
    await page.wait_for_function(
        """() => [...document.querySelectorAll('.chat.active[data-type=chat]')].filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().height).length===1""",
        timeout=10000,
    )
    return active


def menu_intercepted(error):
    return any("btn-menu-overlay" in line and "intercepts pointer events" in line
               for line in str(error).splitlines())


async def dismiss_covering_menu(control):
    """Dismiss only a native menu backdrop covering this navigation control."""
    return await control.evaluate("""button => {
        const box = button.getBoundingClientRect();
        const hit = document.elementFromPoint(box.x + box.width/2, box.y + box.height/2);
        if (!hit?.matches('.btn-menu-overlay')) return false;
        hit.click();
        return true;
    }""", timeout=2500)


async def click_navigation(control, *, timeout=10000, no_wait_after=False):
    """Retry only navigation, once, after identifying the exact blocking menu."""
    try:
        await control.click(timeout=timeout, no_wait_after=no_wait_after)
    except BrowserTimeout as error:
        if not menu_intercepted(error) or not await dismiss_covering_menu(control):
            raise
        await control.click(timeout=timeout, no_wait_after=no_wait_after)


async def close_forum_topics(page):
    """Return from the native topic drawer before using global chat navigation."""
    panel = page.locator(FORUM_PANELS).filter(visible=True)
    if not await panel.count():
        return
    if await panel.count() != 1:
        raise TelegramSetupBlocked("论坛话题页不唯一，无法返回全局搜索")
    menu = panel.locator(".sidebar-header .btn-menu-toggle.menu-open").filter(
        visible=True
    )
    if await menu.count() == 1:
        # Web K can put the dismissible menu backdrop over its own toggle.
        # Activate only the backdrop actually covering this bound menu.
        dismissed = await dismiss_covering_menu(menu)
        if not dismissed:
            await menu.click(timeout=10000)
        try:
            await menu.wait_for(state="hidden", timeout=10000)
        except BrowserTimeout as error:
            if dismissed:
                raise BrowserTimeout("btn-menu-overlay intercepts pointer events: forum menu stayed open after dismissal") from error
            raise
    close = panel.locator(".sidebar-header .sidebar-close-button").filter(visible=True)
    if await close.count() != 1:
        raise TelegramSetupBlocked("无法确认论坛话题返回入口")
    await click_navigation(close, timeout=10000)
    await page.wait_for_function(
        "selector => ![...document.querySelectorAll(selector)].some(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().height)",
        arg=FORUM_PANELS,
        timeout=10000,
    )


async def open_forum_as_messages(page, expected_peer):
    """Use the bound forum's own View as Messages action, without joining it."""
    if not expected_peer or not re.fullmatch(r"-\d+", expected_peer):
        return
    if not await page.locator(".topics-slider,.topics-container").count():
        return
    header = page.locator(
        '.chat.active[data-type="chat"] .chat-info .peer-title[data-peer-id="'
        + expected_peer
        + '"]'
    ).filter(visible=True)
    panel = (
        page.locator(FORUM_PANELS)
        .filter(
            has=page.locator(
                '.sidebar-header .peer-title[data-peer-id="' + expected_peer + '"]'
            )
        )
        .filter(visible=True)
    )
    # A forum route initially leaves the previous group's chat visible while
    # opening its topic drawer. Wait for the bound peer, never use that old chat.
    for _ in range(40):
        if await header.count() == 1:
            return
        if await panel.count():
            break
        await asyncio.sleep(0.25)
    if not await panel.count():
        return  # the caller retains its normal bound-header timeout
    if await panel.count() != 1:
        raise TelegramTargetMismatch("论坛话题页目标不唯一")
    menu = panel.locator(".sidebar-header .btn-menu-toggle").filter(visible=True)
    if await menu.count() != 1:
        raise TelegramSetupBlocked("无法确认论坛显示方式菜单")
    if await menu.get_attribute("aria-expanded") != "true" and not await menu.evaluate("e => e.classList.contains('menu-open')"):
        await click_navigation(menu, timeout=10000)
    messages = (
        page.locator(".btn-menu-item")
        .filter(
            has_text=re.compile(
                r"(?:View|Show) as Messages|以消息形式查看|显示为消息|顯示為訊息",
                re.I,
            )
        )
        .filter(visible=True)
    )
    await messages.wait_for(timeout=5000)
    if await messages.count() != 1:
        raise TelegramSetupBlocked("无法唯一确认论坛消息视图")
    await click_navigation(messages, timeout=10000)
    await header.wait_for(timeout=20000)


async def close_chat_search(page):
    search = (
        (await selected_chat(page)).locator(".topbar-search-input").filter(visible=True)
    )
    if await search.count() == 1:
        await search.fill("")
        clear = (
            (await selected_chat(page))
            .locator(".topbar-search-input-container .input-search-clear")
            .filter(visible=True)
        )
        if await clear.count() != 1:
            raise TelegramSetupBlocked("无法确认群搜索关闭按钮")
        await click_navigation(clear, timeout=10000)
        await search.wait_for(state="hidden", timeout=10000)


async def own_bio_field(page, expected):
    """Navigate only the selected login's Settings → Edit profile form."""
    await open_account(page, expected)
    form = page.locator(".edit-profile-container").filter(visible=True)
    # Telegram Web K's native settings link opens only this login's bio editor.
    await page.evaluate("location.hash = '?tgaddr=' + encodeURIComponent('tg://settings/edit/bio')")
    try:
        await form.wait_for(timeout=9000)
    except BrowserTimeout:
        # The main header appears before Telegram attaches the interactive menu.
        # The outer burger also contains an independent Back button.
        trigger = page.locator("#column-left .sidebar-tools-button.is-visible, #folders-sidebar .btn-menu-toggle").filter(visible=True)
        try:
            await trigger.first.wait_for(timeout=6000)
        except BrowserTimeout:
            trigger = page.locator("#column-left .left-sidebar-burger .sidebar-tools-button, #column-left .left-sidebar-burger .btn-menu-toggle").filter(visible=True)
        await trigger.first.wait_for(timeout=10000)
        if await trigger.count() != 1:
            raise TelegramSetupBlocked("账号主菜单未唯一确认")
        await trigger.click(timeout=8000)
        settings = page.locator('.btn-menu-item:has(.tgico-settings),.btn-menu-item:has([data-i18n="Settings"])').filter(visible=True)
        if not await settings.count():
            settings = page.locator(".btn-menu-item").filter(has_text=re.compile(r"(?:Settings|设置|設定|Ajustes|Настройки)", re.I), visible=True)
        await settings.click(timeout=8000)
        panel = page.locator(".settings-container").filter(visible=True)
        await panel.wait_for(timeout=10000)
        edit = panel.locator(EDIT_BUTTON).filter(visible=True)
        if await edit.count() == 1:
            await edit.click(timeout=8000)
        else:
            await panel.locator(".sidebar-header .btn-menu-toggle").filter(visible=True).click(timeout=8000)
            await page.locator(".btn-menu-item").filter(has_text=re.compile(r"(?:Edit Profile|Edit Account|编辑资料|編輯個人資料)\s*$", re.I), visible=True).click(timeout=8000)
        await form.wait_for(timeout=10000)
    # The editor shell mounts before its account data and labelled fields.
    editable = '[contenteditable=true],textarea,input:not([type=hidden])'
    await form.locator(editable).filter(visible=True).first.wait_for(timeout=15000)
    bio_name = re.compile(r"\b(?:bio(?:graphy)?|about me|about yourself|biografía|biografia)\b|简介|簡介|个人介绍|自我介紹|关于我|關於我|о себе", re.I)
    field = form.locator('[name=bio], [data-name=bio], [data-field=bio]').filter(visible=True)
    if not await field.count():
        field = form.get_by_role("textbox", name=bio_name).filter(visible=True)
    if not await field.count():
        field = form.locator(".input-field").filter(has=page.locator("label,.input-field-label").filter(has_text=bio_name)).locator(editable).filter(visible=True)
    if await field.count() != 1:
        labels = await form.locator("label,.input-field-label,[role=textbox],input,textarea").evaluate_all(
            """els => els.slice(0,14).map(e => [e.tagName, e.getAttribute('name'),e.getAttribute('type'),
              e.getAttribute('aria-label'),e.matches('label,.input-field-label') ? e.textContent?.slice(0,45) : ''].join('/'))""")
        raise TelegramSetupBlocked("个人简介输入框未唯一确认 " + repr(labels)[:450])
    if not await field.evaluate("e=>e.isContentEditable || e.matches('input,textarea')"):
        field = field.locator('[contenteditable=true],textarea,input').filter(visible=True)
    if await field.count() != 1:
        raise TelegramSetupBlocked("个人简介编辑位置变化")
    return form, field


async def profile_panel(page):
    if await page.locator("#column-right").count():
        return page.locator(
            "body.is-right-column-shown #column-right .profile-container.active"
        ).filter(visible=True)
    return page.locator(".profile-container").filter(visible=True).first


async def close_profile(page, profile):
    native = bool(await page.locator("#column-right").count())
    for _ in range(3):
        close = profile.locator(".sidebar-header .sidebar-close-button").filter(
            visible=True
        )
        if await close.count() != 1:
            raise TelegramSetupBlocked("群资料关闭按钮无法唯一确认")
        await click_navigation(close, timeout=10000)
        if not native:
            await profile.wait_for(state="hidden", timeout=10000)
            return
        # First leave shared-media mode, then close the profile. Telegram can
        # ignore a click during its animation; only retry this navigation step.
        await asyncio.sleep(0.65)
        if await page.evaluate(
            "() => !document.body.classList.contains('is-right-column-shown')"
        ):
            return
    raise TelegramSetupBlocked("群资料栏未完成收起，未继续操作聊天")


async def open_selected_profile(page, expected_text=None):
    await close_chat_search(page)
    # Saving Channel Type returns to Edit Channel. A header click alone cannot
    # supersede that left-sidebar tab. Close only the known settings tab first.
    for _ in range(3):
        settings = page.locator(
            ".group-type-container.active,.edit-group-container.active"
        ).filter(visible=True)
        if not await settings.count():
            break
        if await settings.count() != 1:
            raise TelegramSetupBlocked("频道设置页面状态不唯一，已停止操作")
        back = settings.locator(".sidebar-header .sidebar-close-button").filter(
            visible=True
        )
        if await back.count() != 1:
            raise TelegramSetupBlocked("频道设置返回按钮已变化，已停止操作")
        await click_navigation(back, timeout=10000)
        await settings.wait_for(state="hidden", timeout=10000)
    profile = await profile_panel(page)
    if (
        await profile.count()
        and expected_text
        and expected_text.casefold() not in (await profile.inner_text()).casefold()
    ):
        await close_profile(page, profile)
    if not await profile.count() or (
        expected_text and expected_text.casefold() not in (await profile.inner_text()).casefold()
    ):
        chat = await selected_chat(page)
        header = chat.locator(".chat-info").filter(visible=True).first
        title = header.locator(".peer-title[data-peer-id]").filter(visible=True)
        # The header center can sit underneath the floating avatar. The title
        # belongs to the same selected peer and opens the same profile panel.
        # Long titles can extend into the floating actions. The subtitle is
        # bound to the same header and invokes the same native profile action.
        subtitle = header.locator(".content .bottom").filter(visible=True)
        opener = title if await title.count() == 1 else header
        if await subtitle.count() == 1:
            opener = subtitle
        peer = await title.get_attribute("data-peer-id") if await title.count() == 1 else None
        await click_navigation(opener, timeout=10000, no_wait_after=True)
        try:
            await profile.wait_for(timeout=7000)
        except BrowserTimeout:
            # Some group headers use the subtitle for the member list. Return to
            # the chat and open its title instead, keeping the original peer bound.
            if not expected_text or await title.count() != 1 or await subtitle.count() != 1:
                raise
            if not peer or not re.fullmatch(r"-[0-9]+", peer):
                raise
            await close_chat_search(page)
            bound = (await selected_chat(page)).locator('.chat-info .peer-title[data-peer-id="'+peer+'"]').filter(visible=True)
            if await bound.count() != 1:
                raise
            await click_navigation(bound, timeout=8000, no_wait_after=True)
    await profile.wait_for(timeout=15000)
    return profile


async def _own_channel_profile(page, setup):
    # Re-focus the selected peer: cached profile tabs can retain non-zero bounds
    # while another edit tab is on top, so DOM visibility alone is insufficient.
    profile = await open_selected_profile(page, "https://t.me/" + setup["handle"])
    # The deterministic URL in the description identifies our preparation even
    # if the gateway stopped after create and before saving its resulting peer ID.
    if "https://t.me/" + setup["handle"] not in await profile.inner_text():
        raise TelegramSetupBlocked("发现同名频道但无法确认归属，已停止自动修改")
    edit = profile.locator(EDIT_BUTTON).filter(visible=True)
    if await edit.count() != 1:
        raise TelegramSetupBlocked("无法确认此频道由当前账号管理，已停止自动修改")
    return profile


async def skip_new_channel_members(page):
    """Leave optional invitations without submitting a member selection.

    Web K's Next arrow can open an Add Member confirmation even for an empty
    selector. Reconcile that dialog only through Cancel, then leave the sidebar
    through its navigation control. Never treat the submit arrow as Skip.
    """
    confirmation = page.locator(".popup-add-members").filter(visible=True)
    if await confirmation.count():
        if await confirmation.count() != 1:
            raise TelegramSetupBlocked("添加成员确认窗口不唯一，已停止操作")
        cancel = confirmation.locator("button").filter(
            has_text=re.compile(r"^(?:Cancel|取消)$", re.I), visible=True
        )
        if await cancel.count() != 1:
            raise TelegramSetupBlocked("无法确认取消邀请按钮，已停止操作")
        await cancel.click(timeout=10000)
        await confirmation.wait_for(state="hidden", timeout=10000)

    members = page.locator(".add-members-container").filter(visible=True)
    if not await members.count():
        return
    if await members.count() != 1:
        raise TelegramSetupBlocked("创建频道后的邀请页面不唯一，已停止操作")
    close = members.locator(".sidebar-header .sidebar-close-button").filter(
        visible=True
    )
    if await close.count() != 1:
        raise TelegramSetupBlocked("无法确认退出邀请页面的按钮，已停止操作")
    await close.click(timeout=10000)
    await members.wait_for(state="hidden", timeout=10000)


async def find_prepared_channel(page, setup):
    await skip_new_channel_members(page)
    deadline = asyncio.get_running_loop().time() + 8
    found = set()
    while True:
        peers = await page.evaluate(
            r"""title => {
      const ids=new Set();
      for(const el of document.querySelectorAll('.chatlist [data-peer-id]')) {
        const name=el.matches('.peer-title')?el:el.querySelector('.peer-title');
        const id=el.getAttribute('data-peer-id');
        if(name?.textContent.trim()===title && Number(id)<0)ids.add(id);
      }
      // The new peer can already be selected while the virtual chat list is
      // hidden by the creation sidebar. Its unique description is checked below.
      for(const el of document.querySelectorAll('.chat-info .peer-title[data-peer-id]')) {
        if(el.getBoundingClientRect().width && el.textContent.trim()===title && Number(el.dataset.peerId)<0)ids.add(el.dataset.peerId);
      }
      return [...ids];
    }""",
            setup["title"],
        )
        found.update(peers)
        if asyncio.get_running_loop().time() >= deadline:
            break
        # Empty newly-created channels may be below active conversations in a
        # virtual list. Inspect own peer titles only; never open others' messages.
        moved = await page.evaluate("""() => {
          const list=[...document.querySelectorAll('.chatlist,.chatlist-container')].find(e=>e.getBoundingClientRect().width);
          if(!list)return false;
          let scroller=list;
          while(scroller && scroller.scrollHeight<=scroller.clientHeight+1)scroller=scroller.parentElement;
          if(!scroller || scroller===document.body || scroller===document.documentElement)return false;
          const before=scroller.scrollTop;
          scroller.scrollTop+=Math.max(100,scroller.clientHeight*.8);
          return scroller.scrollTop>before;
        }""")
        if not moved:
            break
        await asyncio.sleep(0.4)
    peers = sorted(found)
    search = page.locator("#column-left .input-search-input").filter(visible=True)
    if await search.count() == 1:
        await search.click(timeout=5000)
        await search.fill(setup["title"])
        await asyncio.sleep(0.7)
        try:
            await page.wait_for_function(
                """title => [...document.querySelectorAll('#search-container .peer-title[data-peer-id]')]
              .some(e=>e.textContent.trim()===title && Number(e.dataset.peerId)<0)""",
                arg=setup["title"],
                timeout=8000,
            )
            results = await page.evaluate(
                """title => [...new Set([...document.querySelectorAll('#search-container .peer-title[data-peer-id]')]
              .filter(e=>e.textContent.trim()===title && Number(e.dataset.peerId)<0).map(e=>e.dataset.peerId))]""",
                setup["title"],
            )
            peers = sorted(set(peers) | set(results))
            channels_tab = (
                page.locator("#search-container")
                .get_by_text(re.compile(r"^(Channels|频道)$"))
                .filter(visible=True)
            )
            if await channels_tab.count() == 1:
                await channels_tab.click(timeout=5000)
                await asyncio.sleep(0.7)
                results = await page.evaluate(
                    """title => [...new Set([...document.querySelectorAll('#search-container .peer-title[data-peer-id]')]
                  .filter(e=>e.textContent.trim()===title && Number(e.dataset.peerId)<0).map(e=>e.dataset.peerId))]""",
                    setup["title"],
                )
                peers = sorted(set(peers) | set(results))
        except BrowserTimeout:
            pass
        finally:
            await search.fill("")
    if not peers:
        return None
    own = await inspect_account(page)
    matched = []
    for peer in peers:
        url = "https://web.telegram.org/k/?account=" + own["slot"] + "#" + peer
        try:
            observed = await inspect_target(page, url, own["external_account_id"])
            await _own_channel_profile(page, setup)
            matched.append((peer, observed))
        except (TelegramSetupBlocked, TelegramTargetMismatch):
            # Same title is not identity. Only the task's unique description
            # marker plus verified management permission identifies this channel.
            continue
    if len(matched) > 1:
        raise TelegramSetupBlocked("多个频道含有同一筹备标记，需要核对，未重复创建")
    if not matched:
        return None
    peer, observed = matched[0]
    if peer != peers[-1]:
        await inspect_target(
            page,
            "https://web.telegram.org/k/?account=" + own["slot"] + "#" + peer,
            own["external_account_id"],
        )
        await _own_channel_profile(page, setup)
    return observed


async def fill_new_channel(page, setup):
    """Website UI only. No Bot API, MTProto calls or contact invitations."""
    await (
        page.locator("#new-menu,.btn-new-menu")
        .filter(visible=True)
        .first.click(timeout=10000)
    )
    await (
        page.locator(".btn-menu-item")
        .filter(has_text=re.compile(r"(?:New Channel|新建频道|新建頻道)$", re.I))
        .filter(visible=True)
        .click(timeout=10000)
    )
    form = page.locator(".new-channel-container").filter(visible=True)
    await form.wait_for(timeout=10000)
    fields = form.locator(".input-field-input").filter(visible=True)
    if await fields.count() != 2:
        raise TelegramSetupBlocked(
            "Telegram 创建频道页面已变化，Agent 暂未提交，需要更新适配"
        )
    await fields.nth(0).fill(setup["title"])
    await fields.nth(1).fill(setup["about"])


async def submit_new_channel(page):
    await (
        page.locator(".new-channel-container .btn-corner.is-visible")
        .filter(visible=True)
        .click(timeout=10000)
    )


async def created_channel(page, setup, expected_account):
    # Creating a broadcast opens an optional members selector. Skip with zero
    # selections; never click contacts or invite existing account relationships.
    # Web K first acknowledges creation, then asynchronously mounts either the
    # selected channel or the subscriber picker. The chat list behind it is hidden.
    await page.wait_for_function(
        """title => {
          const visible = el => el.getBoundingClientRect().width && el.getBoundingClientRect().height;
          return [...document.querySelectorAll('.add-members-container,.popup-add-members')].some(visible)
            || [...document.querySelectorAll('.chat-info .peer-title,.chatlist .peer-title')].some(
              el => visible(el) && el.textContent.trim() === title);
        }""",
        arg=setup["title"],
        timeout=25000,
    )
    await skip_new_channel_members(page)
    exact_title = re.compile("^" + re.escape(setup["title"]) + "$")
    header = page.locator(".chat-info .peer-title").filter(has_text=exact_title, visible=True)
    if not await header.count():
        # The header may appear before the list row becomes visible.
        row = page.locator(".chatlist .peer-title").filter(has_text=exact_title, visible=True)
        await header.or_(row).first.wait_for(timeout=20000)
        if not await header.count():
            if await row.count() != 1:
                raise TelegramSetupBlocked("同名频道无法唯一确认，已停止操作")
            await row.click(timeout=10000)
    await page.wait_for_function(
        "title => Array.from(document.querySelectorAll('.chat-info .peer-title[data-peer-id]')).some(e => e.getBoundingClientRect().width && e.textContent.trim()===title && Number(e.dataset.peerId)<0)",
        arg=setup["title"],
        timeout=20000,
    )
    await skip_new_channel_members(page)
    own = await inspect_account(page)
    current = await page.evaluate(TARGET)
    if (
        own["external_account_id"] != expected_account
        or current["title"] != setup["title"]
        or not re.fullmatch(r"-\d+", current.get("target_id") or "")
    ):
        raise TelegramSetupBlocked("创建频道结果尚不能唯一确认，Agent 将核对结果")
    observed = await inspect_target(
        page,
        "https://web.telegram.org/k/?account="
        + own["slot"]
        + "#"
        + current["target_id"],
        expected_account,
    )
    await _own_channel_profile(page, setup)
    return observed


async def fill_public_channel(page, setup):
    profile = await _own_channel_profile(page, setup)
    # Reconciliation can reach an already public channel after the save succeeded
    # but its acknowledgement was lost. Use the dedicated server-backed Link row.
    link = (
        profile.locator(".row.row-grid .row-title")
        .filter(has_text=re.compile("^" + re.escape("t.me/" + setup["handle"]) + "$"))
        .filter(visible=True)
    )
    if await link.count():
        return False
    await profile.locator(EDIT_BUTTON).filter(visible=True).click(timeout=10000)
    await (
        page.locator(".edit-group-container .row")
        .filter(has_text=re.compile(r"Channel Type|频道类型|頻道類型", re.I))
        .filter(visible=True)
        .click(timeout=10000)
    )
    form = page.locator(".group-type-container").filter(visible=True)
    public = form.locator("input[type=radio][value=public]")
    if not await public.is_checked():
        await form.locator("label:has(input[type=radio][value=public])").click(
            timeout=10000
        )
    field = form.locator('input[name="group-public-link"]')
    if await field.input_value() == "t.me/" + setup["handle"]:
        return bool(
            await form.locator(".btn-corner.is-visible").filter(visible=True).count()
        )
    await field.fill("t.me/" + setup["handle"])
    await form.locator('input[name="group-public-link"].valid').wait_for(timeout=15000)
    return True


async def submit_public_channel(page, setup):
    form = page.locator(".group-type-container").filter(visible=True)
    value = await form.locator('input[name="group-public-link"]').input_value()
    if value != "t.me/" + setup["handle"]:
        raise TelegramSetupBlocked("公开频道链接发生变化，已停止提交")
    await form.locator(".btn-corner.is-visible").click(timeout=10000)
    await form.wait_for(state="hidden", timeout=20000)


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
TARGET = r"""() => {const visible=el=>!!(el.getBoundingClientRect().width&&el.getBoundingClientRect().height);const root=Array.from(document.querySelectorAll('.chat.active[data-type=chat]')).find(visible)||document;const header=Array.from(root.querySelectorAll('.chat-info')).find(visible);const peer=header?.querySelector('.peer-title[data-peer-id]')||header?.querySelector('[data-peer-id]');const editor=Array.from(root.querySelectorAll('.input-message-input')).find(el=>visible(el)&&el.isContentEditable&&(!el.dataset.peerId||el.dataset.peerId===peer?.getAttribute('data-peer-id')));const subtitle=header?.innerText||'';const placeholder=editor?.getAttribute('data-placeholder')||editor?.getAttribute('aria-label')||editor?.getAttribute('placeholder')||editor?.parentElement?.innerText||'';return {target_id:peer?.getAttribute('data-peer-id')||null,title:peer?.textContent?.trim()||'',header:subtitle.slice(0,300),audience_subtitle:header?.querySelector('.info .subtitle,.subtitle,.info .status,.bottom,.person')?.innerText||subtitle.split('\n').slice(1).join(' '),can_write:!!editor,placeholder:placeholder.slice(0,200),channel_hint:/subscribers?|订阅者|位订阅|频道|broadcast/i.test(subtitle+' '+placeholder)}}"""


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
    from .public_telegram import audience_counts

    target_url = channel_url(url)
    before = await inspect_account(page)
    if before["external_account_id"] != expected_account:
        raise TelegramTargetMismatch("登录账号已变化，请重新连接账号")
    # A bare /k/ navigation would silently switch a second account back to slot 1.
    p = urlsplit(target_url)
    target_url = p._replace(query=urlencode({"account": before["slot"]})).geturl()
    # Use Web K's in-page routing. Reloading via about:blank repeatedly triggers
    # its service-worker repair redirect and can hide a successfully created peer.
    current = urlsplit(page.url)
    target_url = current._replace(fragment=urlsplit(target_url).fragment).geturl()
    requested_peer = urlsplit(target_url).fragment
    row = (
        page.locator('.chatlist-chat[data-peer-id="' + requested_peer + '"]')
        if re.fullmatch(r"-\d+", requested_peer)
        else None
    )
    if row is not None and await row.filter(visible=True).count():
        await click_navigation(row.filter(visible=True).first)
    else:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
    await (
        page.locator(".chatlist-container,.chatlist")
        .filter(visible=True)
        .first.wait_for(timeout=25000)
    )
    await dismiss_intro(page)
    # Hash navigation completes before Telegram has resolved the peer.
    await (
        (await selected_chat(page)).locator(".chat-info .peer-title[data-peer-id]")
        .filter(visible=True).first
        .wait_for(timeout=20000)
    )
    result = None
    opened_profile = False
    for _ in range(100):
        result = await page.evaluate(TARGET)
        requested = urlsplit(target_url).fragment
        resolved = (
            requested == result.get("target_id")
            if re.fullmatch(r"-\d+", requested)
            else False
        )
        if requested.startswith("@"):
            if not resolved and result["can_write"] and result["channel_hint"]:
                # Current Web K renders the public handle as a Link row, not an
                # anchor, and keeps #@username in the URL. Verify that server-
                # backed row instead of accepting a URL embedded in the bio.
                if not opened_profile:
                    await open_selected_profile(page, "t.me/" + requested[1:])
                    opened_profile = True
                resolved = (
                    await page.locator(".profile-container .row.row-grid .row-title")
                    .filter(
                        has_text=re.compile(
                            "^" + re.escape("t.me/" + requested[1:]) + "$"
                        )
                    )
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
        "audience_count": audience_counts(result.get("audience_subtitle")).get("members"),
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
        "() => Array.from((document.querySelector('.chat.active[data-type=chat]')||document).querySelectorAll('.bubble[data-mid]')).filter(el=>!!(el.getBoundingClientRect().width&&el.getBoundingClientRect().height)).reduce((max,el)=>{try{const id=BigInt(el.dataset.mid);return id>max?id:max}catch{return max}},0n).toString()"
    )


async def fill(page, body):
    if not body.strip() or len(body) > 4096:
        raise ValueError("Telegram 内容长度不符合要求")
    await (
        (await selected_chat(page))
        .locator(".input-message-input[contenteditable=true]")
        .filter(visible=True)
        .fill(body)
    )
    # Web K updates its send/record mode asynchronously after a draft edit.
    await (await selected_chat(page)).locator(".btn-send.send").filter(visible=True).wait_for(timeout=10000)


async def submit(page, *, expected_peer=None):
    # Scope the native button to the selected chat: inactive Web K chats retain
    # geometrically visible send buttons, and Enter may be configured as newline.
    chat = await selected_chat(page)
    if expected_peer is not None:
        if not re.fullmatch(r"-[0-9]+", expected_peer):
            raise TelegramTargetMismatch("无效群编号")
        header = page.locator(f'.chat-info .peer-title[data-peer-id="{expected_peer}"]').filter(visible=True)
        chat = chat.filter(has=header)
    button = chat.locator(".btn-send.send").filter(visible=True)
    if await button.count() != 1:
        raise TelegramSetupBlocked("无法唯一确认当前聊天发送按钮")
    await button.click(timeout=10000)


async def continue_draft(page, body):
    """Continue the persisted draft through native keyboard handling once."""
    editor = (
        (await selected_chat(page))
        .locator(".input-message-input[contenteditable=true]")
        .filter(visible=True)
    )
    if await editor.count() != 1 or " ".join(
        (await editor.inner_text()).split()
    ) != " ".join(body.split()):
        raise TelegramSetupBlocked("原验证草稿已变化，未继续提交")
    await submit(page)


async def unconsumed_probe_proof(
    page, body, minimum_mid, marker, peer, *, require_creation_only=True
):
    """Require fresh scoped search, an unchanged baseline and exact untouched draft."""
    from .community_runtime import latest_position, search_history

    def unchanged(diagnostic):
        return (
            (diagnostic.get("creationServiceOnly") or not require_creation_only)
            and diagnostic.get("draftMatches")
            and diagnostic.get("markerMatches") == 0
            and diagnostic.get("pending") == 0
            and not diagnostic.get("systemDialogs")
        )

    await latest_position(page, peer)
    if await baseline(page) != str(minimum_mid):
        return None
    if not unchanged(await receipt_diagnostic(page, body, marker)):
        return None
    _, search = await search_history(page, marker, peer, 5)
    await close_chat_search(page)
    if search != "no_matches":
        return None
    await latest_position(page, peer)
    diagnostic = await receipt_diagnostic(page, body, marker)
    if await baseline(page) != str(minimum_mid) or not unchanged(diagnostic):
        return None
    return {
        **diagnostic,
        "native_search": search,
        "baseline_unchanged": str(minimum_mid),
        "target_id": peer,
    }


def rendered_text(body):
    """Telegram may render supported Markdown delimiters before confirming text."""
    for pattern in (r"(\*\*|__|~~|\|\|)(.+?)\1", r"(`{1,3})(.+?)\1"):
        body = re.sub(pattern, lambda match: match[2], body, flags=re.S)
    return body


async def receipt(page, body, minimum_mid, marker):
    if not marker or marker not in body:
        raise ValueError("发布缺少可核对的唯一标识")
    # Returns only the matching own content ID, never other messages or chat text.
    return await page.evaluate(
        r"""({bodies,minimum,marker})=>{const normalize=s=>s.replace(/\s+/g,' ').trim();const matches=Array.from((document.querySelector('.chat.active[data-type=chat]')||document).querySelectorAll('.bubble[data-mid]')).filter(el=>{if(!(el.getBoundingClientRect().width&&el.getBoundingClientRect().height))return false;let mid;try{mid=BigInt(el.dataset.mid)}catch{return false}const message=el.querySelector('.message,.message-text');return mid>BigInt(minimum)&&message&&message.innerText.includes(marker)&&bodies.some(body=>normalize(message.innerText).startsWith(normalize(body)))&&!el.matches('.is-sending,.sending,.is-error,.error')&&!el.querySelector('.sending,.is-sending,.tgico-sending,.tgico-sendingerror')});return matches.length===1?{mid:matches[0].dataset.mid}:null}""",
        {
            "bodies": [body, rendered_text(body)],
            "minimum": str(minimum_mid),
            "marker": marker,
        },
    )


async def receipt_diagnostic(page, body, marker):
    return await page.evaluate(
        r"""({body,marker})=>{const visible=e=>!!(e.getBoundingClientRect().width&&e.getBoundingClientRect().height);const norm=s=>s.replace(/\s+/g,' ').trim();const bubbles=[...(document.querySelector('.chat.active[data-type=chat]')||document).querySelectorAll('.bubble[data-mid]')].filter(visible);const matching=bubbles.filter(e=>e.querySelector('.message,.message-text')?.innerText.includes(marker));return {visibleBubbles:bubbles.length,bubbleClasses:bubbles.map(e=>e.className),creationServiceOnly:bubbles.length===1&&/Channel created|You created|创建了频道|频道已创建|建立了頻道/i.test(bubbles[0].innerText),markerMatches:matching.length,bodyMatches:matching.filter(e=>norm(e.querySelector('.message,.message-text').innerText).startsWith(norm(body))).length,matchingMids:matching.map(e=>e.dataset.mid),pending:matching.filter(e=>e.matches('.is-sending,.sending,.is-error,.error')||e.querySelector('.sending,.is-sending,.tgico-sending,.tgico-sendingerror')).length,failedMatching:matching.filter(e=>e.classList.contains('is-out')&&(e.matches('.is-error,.error')||e.querySelector('.tgico-sendingerror'))&&norm(e.querySelector('.message,.message-text').innerText).startsWith(norm(body))).length,draftMatches:[...(document.querySelector('.chat.active[data-type=chat]')||document).querySelectorAll('.input-message-input[contenteditable=true]')].filter(visible).some(e=>norm(e.innerText)===norm(body)),systemDialogs:[...document.querySelectorAll('.popup-container')].filter(visible).map(e=>e.textContent.slice(0,350))}}""",
        {"body": body, "marker": marker},
    )
