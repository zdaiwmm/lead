"""Concrete website recipes. External writes remain behind the actor ledger.

Every selector recipe fails closed on ambiguity; no network posting APIs, injected
platform internals, arbitrary JavaScript from the model, or automatic login.
"""

import hashlib
import hmac
import re
from urllib.parse import parse_qs, urlsplit

from .catalog import SPECS, target_url, validate
from .core import (
    LayoutChanged,
    NeedsAuth,
    ScopeChanged,
    check_surface,
    click,
    fill,
    first_visible,
    local_account_link,
    optional_click,
    scoped_receipt,
    upload_files,
    verify_fields,
    wait_ready,
)

ACCOUNT_LINKS = {
    "instagram": (
        'nav a:has(img[alt*="profile picture"])',
        'a[aria-label="Profile"]',
        'a:has(svg[aria-label="Profile"])',
    ),
    "youtube": (
        "ytcp-app a#channel-url",
        'a[aria-label="View channel"]',
        'ytd-active-account-header-renderer a[href*="/channel/"]',
    ),
    "tiktok": ('a[data-e2e="nav-profile"]', 'a[data-e2e="nav-profile-link"]'),
    "x": ('a[data-testid="AppTabBar_Profile_Link"]',),
    "linkedin": (
        'a[data-control-name="identity_welcome_message"]',
        'a[data-control-name="nav.settings_view_profile"]',
    ),
    "reddit": ('header a[href*="/user/"]', 'reddit-header a[href*="/user/"]'),
    "pinterest": ('a[data-test-id="header-profile"]', 'a[data-test-id="profile-link"]'),
    "threads": ('a[aria-label="Profile"]', 'a:has(svg[aria-label="Profile"])'),
}
EDITORS = {
    "facebook": (
        '[role="dialog"] [contenteditable=true][role=textbox]',
        '[contenteditable=true][aria-label*="Write"]',
    ),
    "instagram": (
        'textarea[aria-label="Write a caption..."]',
        '[contenteditable=true][aria-label*="caption"]',
    ),
    "tiktok": (
        '[contenteditable=true][data-e2e="caption-editor"]',
        ".public-DraftEditor-content[contenteditable=true]",
    ),
    "x": ('[data-testid="tweetTextarea_0"]',),
    "linkedin": (
        '[role="dialog"] .ql-editor[contenteditable=true]',
        ".ql-editor[contenteditable=true][role=textbox]",
    ),
    "reddit": ("[contenteditable=true][role=textbox]", "textarea[name=body]"),
    "pinterest": (
        '[contenteditable=true][data-test-id="pin-draft-description"]',
        'textarea[placeholder*="description"]',
    ),
    "threads": (
        "[role=dialog] [contenteditable=true][role=textbox]",
        '[contenteditable=true][aria-label*="thread"]',
    ),
    "whatsapp": (
        '[contenteditable=true][data-tab="10"]',
        '[contenteditable=true][aria-label="Write an update"]',
    ),
}
FINALS = {
    "facebook": (
        '[role=dialog] [role=button][aria-label="Post"]',
        'button:text-is("Publish")',
    ),
    "instagram": (
        '[role=dialog] button:text-is("Share")',
        '[role=dialog] [role=button]:text-is("Share")',
    ),
    "youtube": ("ytcp-button#done-button", 'button:text-is("Publish")'),
    "tiktok": ('button[data-e2e="post_video_button"]', 'button:text-is("Post")'),
    "x": ('[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]'),
    "linkedin": (
        "button.share-actions__primary-action",
        '[role=dialog] button:text-is("Post")',
    ),
    "reddit": ('button[type=submit]:text-is("Post")', "button#submit-post-button"),
    "pinterest": (
        'button[data-test-id="board-dropdown-save-button"]',
        'button[data-test-id="pin-draft-submit-button"]',
    ),
    "threads": (
        '[role=dialog] [role=button]:text-is("Post")',
        '[role=dialog] button:text-is("Post")',
    ),
    "whatsapp": ('button[aria-label="Send"]', '[role=button][aria-label="Send"]'),
}
RECEIPTS = {
    "facebook": (
        "[role=article]",
        r"facebook\.com/(?:[^/]+/posts/[^/?]+|permalink\.php\?|story\.php\?)",
        r"facebook\.com/",
    ),
    "instagram": (
        "article",
        r"instagram\.com/(?:p|reel)/[A-Za-z0-9_-]+",
        r"instagram\.com/",
    ),
    "x": ("article[data-testid=tweet]", r"x\.com/[^/]+/status/\d+", r"x\.com/"),
    "linkedin": (
        '[data-urn^="urn:li:activity:"]',
        r"linkedin\.com/(?:feed/update/urn:li:activity:|posts/)",
        r"linkedin\.com/(?:in|company)/",
    ),
    "reddit": (
        "shreddit-post,article",
        r"reddit\.com/r/[^/]+/comments/[A-Za-z0-9]+",
        r"reddit\.com/user/",
    ),
    "pinterest": (
        '[data-test-id="pin"]',
        r"pinterest\.com/pin/\d+",
        r"pinterest\.com/",
    ),
    "threads": (
        "[data-pressable-container=true],article",
        r"threads\.(?:com|net)/@[^/]+/post/[A-Za-z0-9_-]+",
        r"threads\.(?:com|net)/@",
    ),
    "tiktok": (
        '[data-e2e="content-post-row"],[data-tt="components_PostTable_Container"]',
        r"tiktok\.com/@[^/]+/video/\d+",
        r"tiktok\.com/@",
    ),
}


class WebAdapter:
    def __init__(self, platform):
        self.platform = platform
        self.spec = SPECS[platform]

    @property
    def hosts(self):
        return (
            {"www.youtube.com", "studio.youtube.com"}
            if self.platform == "youtube"
            else {"web.whatsapp.com"}
            if self.platform == "whatsapp"
            else {
                self.spec.host,
                self.spec.host.removeprefix("www."),
                *(
                    {"threads.net", "www.threads.net"}
                    if self.platform == "threads"
                    else set()
                ),
            }
        )

    async def account(self, page):
        await check_surface(page, self.hosts)
        if self.platform == "facebook":
            cookies = await page.context.cookies(["https://www.facebook.com"])
            own = next((c["value"] for c in cookies if c["name"] == "c_user"), None)
            if (
                not own
                or not own.isdigit()
                or not await page.locator(
                    '[aria-label="Your profile"],[aria-label="Account"]'
                )
                .filter(visible=True)
                .count()
            ):
                raise NeedsAuth("请先登录 Facebook 并打开自己的主页")
            identity = own
        elif self.platform == "whatsapp":
            # Only the current owner's WID, never contacts, chats or auth keys.
            raw = await page.evaluate(
                "()=>{try{return JSON.parse(localStorage.getItem('last-wid')||'null')?.split(':')[0]?.split('@')[0]||null}catch{return null}}"
            )
            if (
                not raw
                or not re.fullmatch(r"\d+", raw)
                or not await page.locator('[aria-label="Channels"],[aria-label="频道"]')
                .filter(visible=True)
                .count()
            ):
                raise NeedsAuth("请先登录 WhatsApp Web")
            from ..config import CONFIG

            identity = hmac.new(
                CONFIG["session_secret"].encode(),
                ("whatsapp:" + raw).encode(),
                hashlib.sha256,
            ).hexdigest()
        else:
            try:
                href = await local_account_link(page, ACCOUNT_LINKS[self.platform])
            except NeedsAuth:
                menus = {
                    "youtube": (
                        "button#avatar-btn",
                        'button[aria-label="Account menu"]',
                    ),
                    "linkedin": ("button.global-nav__primary-link-me-menu-trigger",),
                }
                if self.platform not in menus:
                    raise
                await click(page, menus[self.platform])
                href = await local_account_link(page, ACCOUNT_LINKS[self.platform])
            p = urlsplit(
                href
                if href.startswith("https://")
                else "https://" + self.spec.host + href
            )
            if p.hostname not in self.hosts and not (
                self.platform == "youtube" and p.hostname == "www.youtube.com"
            ):
                raise ScopeChanged("当前账号链接不属于此平台")
            parts = p.path.strip("/").split("/")
            if self.platform == "youtube":
                if (
                    len(parts) < 2
                    or parts[0] != "channel"
                    or not re.fullmatch(r"UC[A-Za-z0-9_-]{20,30}", parts[1])
                ):
                    raise NeedsAuth("请在 YouTube 账号菜单中打开当前频道")
                identity = parts[1]
            elif self.platform == "linkedin":
                if parts[0] != "in" or len(parts) < 2:
                    raise NeedsAuth("无法核对当前 LinkedIn 身份")
                identity = parts[1]
            elif self.platform == "reddit":
                if parts[0] != "user" or len(parts) < 2:
                    raise NeedsAuth("无法核对当前 Reddit 身份")
                identity = parts[1]
            else:
                identity = parts[0].removeprefix("@")
            if not identity or not re.fullmatch(r"[A-Za-z0-9_.%\-]+", identity):
                raise NeedsAuth("无法核对稳定账号标识")
        return {
            "platform": self.platform,
            "external_account_id": identity,
            "display_name": self.platform
            + " · "
            + (identity[:10] if self.platform == "whatsapp" else identity),
        }

    async def target(self, page, url, expected_account):
        canonical = target_url(self.platform, url)
        own = await self.account(page)
        if own["external_account_id"] != expected_account:
            raise ScopeChanged("登录身份已改变")
        p = urlsplit(canonical)
        parts = p.path.strip("/").split("/")
        if self.platform in ("x", "instagram", "tiktok", "threads"):
            target_id = parts[0].removeprefix("@")
            if target_id.lower() != expected_account.lower():
                raise ScopeChanged("当前仅运营已绑定账号自己的主页")
        elif self.platform == "youtube":
            target_id = parts[1]
            if target_id != expected_account:
                raise ScopeChanged("YouTube 当前频道与授权目标不一致")
        elif self.platform == "linkedin":
            target_id = parts[1]
            if parts[0] == "in" and target_id.lower() != expected_account.lower():
                raise ScopeChanged("当前个人主页不是已绑定账号")
        elif self.platform == "pinterest":
            if parts[0].lower() != expected_account.lower():
                raise ScopeChanged("只能运营当前账号自己的画板")
            target_id = "/".join(parts[:2])
        elif self.platform == "reddit":
            target_id = parts[1]
        elif self.platform == "facebook":
            target_id = (
                parse_qs(p.query)["id"][0] if p.path == "/profile.php" else parts[0]
            )
        else:
            target_id = parts[1]
            await click(page, ('[aria-label="Channels"]', '[aria-label="频道"]'))
            await optional_click(page, (f'[data-channel-id="{target_id}"]',))
            await optional_click(page, ('[aria-label="Channel info"]',))
            link = page.locator(
                f'a[href="{canonical.rstrip("/")}"],a[href="{canonical}"]'
            ).filter(visible=True)
            if not await link.count():
                raise ScopeChanged(
                    "请在 WhatsApp 中打开自有频道并显示频道链接，以核对发布目标"
                )
            await optional_click(
                page, ('button[aria-label="Close"]', '[role=button][aria-label="Back"]')
            )
            await first_visible(page, EDITORS["whatsapp"])
            return {
                "external_target_id": target_id,
                "display_name": target_id,
                "canonical_url": canonical,
                "public_url": canonical,
                "target_type": "channel",
                "verified_permissions": ["publish", "upload_media"],
                "adapter_version": self.spec.version,
            }
        # A fresh document prevents matching a previous account/target's stale DOM.
        await page.goto("about:blank")
        await page.goto(canonical, wait_until="domcontentloaded", timeout=45000)
        after = await self.account(page)
        if after["external_account_id"] != expected_account:
            raise ScopeChanged("目标打开后登录身份发生变化")
        if self.platform == "facebook":
            # A Page must expose management UI as well as its own posting composer.
            if (
                not await page.locator(
                    'a[href*="professional_dashboard"],a[href*="manage"],[aria-label="Manage Page"]'
                )
                .filter(visible=True)
                .count()
            ):
                raise ScopeChanged("无法确认 Facebook Page 管理权限")
        if (
            self.platform == "reddit"
            and not await page.locator(
                f'a[href*="/mod/{target_id}"],a[href*="/r/{target_id}/about/modqueue"]'
            )
            .filter(visible=True)
            .count()
        ):
            raise ScopeChanged("当前仅开放自有或有版主管理权限的社区")
        if (
            self.platform == "linkedin"
            and parts[0] == "company"
            and not await page.locator(
                'a[href*="/admin/"],button:has-text("Create a post")'
            )
            .filter(visible=True)
            .count()
        ):
            raise ScopeChanged("无法确认 LinkedIn Page 管理权限")
        name = (
            await page.locator("h1").first.inner_text()
            if await page.locator("h1").count()
            else target_id
        )
        return {
            "external_target_id": target_id,
            "display_name": name[:120],
            "canonical_url": canonical,
            "public_url": canonical,
            "target_type": self.spec.target_type
            if self.platform != "linkedin" or parts[0] == "in"
            else "page",
            "verified_permissions": ["publish", "upload_media"],
            "adapter_version": self.spec.version,
        }

    async def prepare(self, page, target, publication, media, paths):
        kind = validate(self.platform, publication, media)
        if (
            kind == "video"
            and self.platform not in ("youtube",)
            and (
                media[0]["media_type"] != "video/mp4"
                or media[0]["metadata"].get("codec") != "h264"
            )
        ):
            raise ValueError("此平台当前使用 MP4 / H.264 视频")
        own = await self.account(page)
        await self.target(page, target["canonical_url"], own["external_account_id"])
        platform = self.platform
        observation = {"kind": kind}
        if platform == "x":
            await page.goto("https://x.com/compose/post", wait_until="domcontentloaded")
        elif platform == "facebook":
            await click(
                page,
                (
                    '[role=button]:text-is("Create post")',
                    '[role=button]:has-text("What’s on your mind")',
                    'button:text-is("Create post")',
                ),
            )
        elif platform == "linkedin":
            await click(
                page,
                (
                    "button.share-box-feed-entry__trigger",
                    'button:has-text("Start a post")',
                    'button:has-text("Create a post")',
                ),
            )
        elif platform == "threads":
            await click(
                page,
                (
                    '[role=button][aria-label="Create"]',
                    'button[aria-label="Create"]',
                    'a[aria-label="Create"]',
                ),
            )
        elif platform == "reddit":
            await page.goto(
                target["canonical_url"].rstrip("/") + "/submit",
                wait_until="domcontentloaded",
            )
            if kind != "text":
                await click(
                    page,
                    (
                        'button:text-is("Images & Video")',
                        '[role=tab]:has-text("Images & Video")',
                    ),
                )
            await fill(
                page,
                ("textarea[name=title]", "input[name=title]"),
                publication["title"],
            )
        elif platform == "instagram":
            await click(
                page,
                (
                    'a:has(svg[aria-label="New post"])',
                    '[role=button]:has(svg[aria-label="New post"])',
                    '[aria-label="Create"]',
                ),
            )
            await optional_click(page, ('[role=menuitem]:text-is("Post")',))
        elif platform == "pinterest":
            await page.goto(
                "https://www.pinterest.com/pin-creation-tool/",
                wait_until="domcontentloaded",
            )
            await fill(
                page,
                (
                    'textarea[data-test-id="pin-draft-title"]',
                    'input[placeholder*="title"]',
                ),
                publication["title"],
            )
        elif platform == "tiktok":
            await page.goto(
                "https://www.tiktok.com/tiktokstudio/upload",
                wait_until="domcontentloaded",
            )
        elif platform == "youtube":
            if kind == "video":
                return await self.youtube_video(page, target, publication, media, paths)
            await page.goto(
                target["canonical_url"].rstrip("/") + "/community",
                wait_until="domcontentloaded",
            )
            await click(
                page,
                (
                    "ytd-post-creation-box-renderer [role=textbox]",
                    '[role=button]:text-is("Create post")',
                ),
            )
            await click(page, ('button[aria-label="Image"]',))
        if paths:
            if platform == "facebook":
                await optional_click(
                    page, ('[aria-label="Photo/video"]', '[aria-label="Photo/Video"]')
                )
            if platform == "linkedin":
                await optional_click(
                    page,
                    (
                        'button[aria-label="Add media"]',
                        'button[aria-label="Add a photo"]',
                        'button[aria-label="Add a video"]',
                    ),
                )
            if platform == "whatsapp":
                await click(
                    page,
                    (
                        'button[aria-label="Attach"]',
                        '[role=button][aria-label="Attach"]',
                    ),
                )
            await upload_files(page, paths)
            await wait_ready(page)
            if platform == "instagram":
                await click(
                    page,
                    (
                        '[role=dialog] button:text-is("Next")',
                        '[role=dialog] [role=button]:text-is("Next")',
                    ),
                )
                await optional_click(
                    page,
                    (
                        '[role=dialog] button:text-is("Next")',
                        '[role=dialog] [role=button]:text-is("Next")',
                    ),
                )
            if platform == "linkedin":
                await optional_click(page, ('[role=dialog] button:text-is("Next")',))
        if platform == "youtube":
            await fill(
                page,
                ("ytd-post-creation-box-renderer [contenteditable=true]",),
                publication["body"],
            )
            final = ('ytd-post-creation-box-renderer button[aria-label="Post"]',)
        else:
            if platform != "reddit" or kind == "text":
                await fill(page, EDITORS[platform], publication["body"])
            final = FINALS[platform]
        if platform == "reddit" and kind != "text":
            # Reddit's native image/video post has a title. The unique public
            # reference must be in that visible title when no body is supported.
            if publication["marker"] not in publication["title"]:
                raise LayoutChanged("图片/视频帖标题缺少可核对标记")
        if platform == "pinterest":
            await click(page, ('[data-test-id="board-dropdown-select-button"]',))
            board = target["external_target_id"].split("/")[-1]
            await click(
                page,
                (
                    f'[role=option][data-board-slug="{board}"]',
                    f'[data-test-id="board-dropdown-board-item"]:has-text("{target["display_name"]}")',
                ),
            )
            if publication.get("link"):
                await fill(
                    page,
                    (
                        'input[data-test-id="website-link"]',
                        'input[placeholder*="link"]',
                    ),
                    publication["link"],
                )
        if platform == "tiktok":
            await optional_click(page, ('button:has-text("Show more")',))
            await (
                await first_visible(
                    page,
                    (
                        'input[name="commercial_content"]',
                        '[role=checkbox][aria-label="Disclose post content"]',
                    ),
                )
            ).check()
            await (
                await first_visible(
                    page,
                    (
                        'input[value="own_brand"]',
                        '[role=checkbox][aria-label="Your brand"]',
                    ),
                )
            ).check()
            if media[0]["rights"]["ai_generated"]:
                await (
                    await first_visible(
                        page,
                        (
                            'input[name="ai_generated"]',
                            '[role=checkbox][aria-label="AI-generated content"]',
                        ),
                    )
                ).check()
        button = await first_visible(page, final)
        if not await button.is_enabled():
            raise LayoutChanged("媒体未准备完成或必填信息缺失，未进入提交阶段")
        observation["final_selectors"] = list(final)
        observation["prepared_url"] = page.url
        observation["account_id"] = own["external_account_id"]
        observation["target_id"] = target["external_target_id"]
        observation["fields"] = []
        if platform == "reddit":
            observation["fields"].append(
                {
                    "selectors": ["textarea[name=title]", "input[name=title]"],
                    "value": publication["title"],
                }
            )
        if platform == "pinterest":
            observation["fields"].append(
                {
                    "selectors": [
                        'textarea[data-test-id="pin-draft-title"]',
                        'input[placeholder*="title"]',
                    ],
                    "value": publication["title"],
                }
            )
        if platform != "reddit" or kind == "text":
            observation["fields"].append(
                {
                    "selectors": [
                        "ytd-post-creation-box-renderer [contenteditable=true]"
                    ]
                    if platform == "youtube"
                    else list(EDITORS[platform]),
                    "value": publication["body"],
                }
            )
        return observation

    async def youtube_video(self, page, target, publication, media, paths):
        channel = target["external_target_id"]
        await page.goto(
            f"https://studio.youtube.com/channel/{channel}/videos/upload",
            wait_until="domcontentloaded",
        )
        await optional_click(
            page, ("ytcp-button#create-icon", 'button[aria-label="Create"]')
        )
        await optional_click(page, ('tp-yt-paper-item:has-text("Upload videos")',))
        await upload_files(page, paths)
        await fill(
            page,
            (
                "ytcp-social-suggestions-textbox#title-textarea [contenteditable=true]",
                "#title-textarea [contenteditable=true]",
            ),
            publication["title"],
        )
        await fill(
            page,
            (
                "ytcp-social-suggestions-textbox#description-textarea [contenteditable=true]",
                "#description-textarea [contenteditable=true]",
            ),
            publication["body"],
        )
        kids = (
            "VIDEO_MADE_FOR_KIDS_MFK"
            if publication["made_for_kids"]
            else "VIDEO_MADE_FOR_KIDS_NOT_MFK"
        )
        await click(
            page,
            (
                f'tp-yt-paper-radio-button[name="{kids}"]',
                f'input[name="made_for_kids"][value="{str(publication["made_for_kids"]).lower()}"]',
            ),
        )
        if media[0]["rights"]["ai_generated"]:
            await optional_click(
                page, ('ytcp-button#toggle-button:has-text("Show more")',)
            )
            await click(
                page,
                (
                    'input[name="altered_content"][value="yes"]',
                    'tp-yt-paper-radio-button[name="ALTERED_CONTENT_YES"]',
                ),
            )
        # Elements and checks are separate screens. No repeated blind Next clicks.
        for stage in range(3):
            await click(page, ("ytcp-button#next-button", "button#next-button"))
        await click(
            page,
            (
                'tp-yt-paper-radio-button[name="PUBLIC"]',
                'input[name="visibility"][value="public"]',
            ),
        )
        await wait_ready(page)
        links = await page.locator(
            'ytcp-uploads-dialog a[href*="youtu.be/"],ytcp-uploads-dialog a[href*="youtube.com/watch?v="]'
        ).all()
        ids = set()
        for link in links:
            href = await link.get_attribute("href")
            if href:
                p = urlsplit(href)
                identifier = (
                    p.path.strip("/")
                    if p.hostname == "youtu.be"
                    else parse_qs(p.query).get("v", [""])[0]
                )
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
                    ids.add(identifier)
        if len(ids) != 1:
            raise LayoutChanged("无法核对当前上传草稿的视频 ID")
        final = await first_visible(page, FINALS["youtube"])
        if not await final.is_enabled():
            raise LayoutChanged("视频处理或检查尚未完成")
        return {
            "kind": "video",
            "fields": [
                {
                    "selectors": [
                        "ytcp-social-suggestions-textbox#title-textarea [contenteditable=true]",
                        "#title-textarea [contenteditable=true]",
                    ],
                    "value": publication["title"],
                },
                {
                    "selectors": [
                        "ytcp-social-suggestions-textbox#description-textarea [contenteditable=true]",
                        "#description-textarea [contenteditable=true]",
                    ],
                    "value": publication["body"],
                },
            ],
            "video_id": next(iter(ids)),
            "final_selectors": list(FINALS["youtube"]),
            "prepared_url": page.url,
            "account_id": target["external_target_id"],
            "target_id": target["external_target_id"],
        }

    async def before_commit(self, page, target, observation, expected_account):
        own = await self.account(page)
        if own["external_account_id"] != expected_account:
            raise ScopeChanged("提交前账号已改变")
        if observation["target_id"] != target["external_target_id"]:
            raise ScopeChanged("提交目标已改变")
        current = urlsplit(page.url)
        prepared = urlsplit(observation["prepared_url"])
        if (current.hostname, current.path, current.query) != (
            prepared.hostname,
            prepared.path,
            prepared.query,
        ):
            raise ScopeChanged("准备完成后页面已改变")
        button = await first_visible(page, observation["final_selectors"], timeout=1000)
        if not await button.is_enabled():
            raise LayoutChanged("发布按钮不再可用")
        await verify_fields(page, observation["fields"])

    async def submit(self, page, observation):
        await click(page, observation["final_selectors"])

    async def receipt(self, page, target, publication, observation, expected_account):
        own = await self.account(page)
        if own["external_account_id"] != expected_account:
            raise ScopeChanged("核对时账号已改变，禁止重发")
        if self.platform == "youtube":
            if observation.get("kind") == "video":
                identifier = observation.get("video_id")
                if not identifier:
                    return None
                result = await page.evaluate(
                    """({id})=>{const root=document.querySelector('ytcp-video-share-dialog,ytcp-video-row[data-video-id="'+id+'"]');if(!root)return null;const link=Array.from(root.querySelectorAll('a[href]')).find(a=>a.href.includes(id));const done=root.querySelector('[data-visibility="public"],[aria-label="Public"]');return link&&done?{external_id:id,url:'https://www.youtube.com/watch?v='+id}:null}""",
                    {"id": identifier},
                )
                return result
            return await scoped_receipt(
                page,
                "ytd-backstage-post-thread-renderer",
                r"youtube\.com/post/[A-Za-z0-9_-]+",
                publication,
                expected_account,
                r"youtube\.com/channel/",
            )
        if self.platform == "whatsapp":
            # Match only the already known own update, including its canonical
            # channel-update link. An apparent Send success is insufficient.
            return await scoped_receipt(
                page,
                "[data-id][data-channel-update],article",
                r"whatsapp\.com/channel/[A-Za-z0-9]+/\d+",
                publication,
                target["external_target_id"],
                r"whatsapp\.com/channel/",
            )
        containers, pattern, author = RECEIPTS[self.platform]
        match = publication
        if self.platform == "reddit" and observation.get("kind") != "text":
            match = {**publication, "body": publication["title"]}
        return await scoped_receipt(
            page,
            containers,
            pattern,
            match,
            expected_account
            if self.platform != "facebook"
            else target["external_target_id"],
            author,
            {
                "x": '[data-testid="User-Name"] a,header a',
                "facebook": '[data-ad-rendering-role="profile_name"] a,h2 a,h3 a,header a',
                "instagram": "header a",
                "linkedin": ".update-components-actor__meta-link,.feed-shared-actor__container-link,header a",
                "reddit": 'a[slot="authorName"],a[data-testid="post_author_link"],header a',
                "pinterest": '[data-test-id="pin-creator-link"],header a',
                "threads": "header a,[role=heading] a",
                "tiktok": '[data-e2e="post-author"] a,a[href*="/video/"],header a',
            }[self.platform],
        )

    async def reconcile(self, page, target, publication, observation, expected_account):
        # Read the known own target; never open a new composer or upload again.
        if self.platform == "youtube" and observation.get("video_id"):
            await page.goto(
                f"https://studio.youtube.com/channel/{target['external_target_id']}/videos/upload",
                wait_until="domcontentloaded",
            )
        elif self.platform != "whatsapp":
            await page.goto(target["canonical_url"], wait_until="domcontentloaded")
        return await self.receipt(
            page, target, publication, observation, expected_account
        )
