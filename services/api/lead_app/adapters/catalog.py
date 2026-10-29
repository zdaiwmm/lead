"""Versioned website contracts. Limits below are lead's conservative limits.

Specifications are not claims of live-account acceptance. Each target and media
format must earn a separate, evidence-backed live probe before unattended use.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Spec:
    platform: str
    host: str
    target_type: str
    target_pattern: str
    formats: tuple[str, ...]
    text_limit: int
    image_count: int
    image_bytes: int = 20 * 1024**2
    video_seconds: int = 120
    title_required: bool = False
    policy: str = "pending"
    source: str = ""
    version: str = "web-2"


SPECS = {
    s.platform: s
    for s in (
        Spec(
            "facebook",
            "www.facebook.com",
            "page",
            r"/(?:profile\.php|[A-Za-z0-9.]+/?)",
            ("text", "image", "video"),
            5000,
            4,
            source="https://www.facebook.com/business/help/1129470964230971",
        ),
        Spec(
            "instagram",
            "www.instagram.com",
            "profile",
            r"/[A-Za-z0-9_.]+/?",
            ("image", "video"),
            2000,
            10,
            source="https://help.instagram.com/442418472487929",
        ),
        Spec(
            "youtube",
            "www.youtube.com",
            "channel",
            r"/channel/UC[A-Za-z0-9_-]{20,30}/?(?:community)?",
            ("image", "video"),
            4000,
            1,
            title_required=True,
            video_seconds=600,
            source="https://support.google.com/youtube/answer/57407",
        ),
        Spec(
            "tiktok",
            "www.tiktok.com",
            "profile",
            r"/@[A-Za-z0-9_.]+/?",
            ("video",),
            2000,
            0,
            title_required=False,
            source="https://support.tiktok.com/en/business-and-creator/creator-and-business-accounts/promoting-a-brand-product-or-service",
        ),
        Spec(
            "x",
            "x.com",
            "profile",
            r"/[A-Za-z0-9_]{1,15}/?",
            ("text", "image", "video"),
            280,
            4,
            policy="restricted",
            source="https://help.x.com/en/using-x/how-to-post",
        ),
        Spec(
            "linkedin",
            "www.linkedin.com",
            "profile",
            r"/(?:in|company)/[A-Za-z0-9_%.-]+/?(?:admin/.*)?",
            ("text", "image", "video"),
            2800,
            4,
            image_bytes=5 * 1024**2,
            policy="restricted",
            source="https://www.linkedin.com/help/linkedin/answer/a518996/",
        ),
        Spec(
            "reddit",
            "www.reddit.com",
            "subreddit",
            r"/r/[A-Za-z0-9_]{3,21}/?",
            ("text", "image", "video"),
            10000,
            4,
            title_required=True,
            source="https://support.reddithelp.com/hc/en-us/articles/360060422572-How-do-I-post-on-Reddit",
        ),
        Spec(
            "pinterest",
            "www.pinterest.com",
            "board",
            r"/[A-Za-z0-9_]+/[A-Za-z0-9_-]+/?",
            ("image", "video"),
            500,
            1,
            title_required=True,
            source="https://help.pinterest.com/en/article/create-a-pin-from-an-image-or-video",
        ),
        Spec(
            "threads",
            "www.threads.com",
            "profile",
            r"/@[A-Za-z0-9_.]+/?",
            ("text", "image", "video"),
            500,
            4,
            source="https://about.fb.com/news/2023/07/introducing-threads-new-app-text-sharing/",
        ),
        Spec(
            "whatsapp",
            "www.whatsapp.com",
            "channel",
            r"/channel/[A-Za-z0-9]+/?",
            ("text", "image", "video"),
            2000,
            1,
            source="https://faq.whatsapp.com/265055289421317",
        ),
    )
}


SPECS["telegram"] = Spec(
    "telegram",
    "web.telegram.org",
    "channel",
    r"/k/",
    ("text", "image", "video"),
    4096,
    4,
    version="telegram-webk-2",
    source="https://telegram.org/tos",
)


def target_url(platform, value):
    if platform == "telegram":
        from ..telegram import channel_url

        return channel_url(value)
    spec = SPECS[platform]
    p = urlsplit(value.strip())
    aliases = {spec.host, spec.host.removeprefix("www.")}
    if platform == "threads":
        aliases |= {"threads.net", "www.threads.net"}
    if (
        p.scheme != "https"
        or p.hostname not in aliases
        or p.username
        or p.password
        or p.port not in (None, 443)
        or p.fragment
        or not re.fullmatch(spec.target_pattern, p.path)
    ):
        raise ValueError("请提供此平台上你有运营权限的目标链接")
    if platform == "facebook" and p.path == "/profile.php":
        from urllib.parse import parse_qs

        account = parse_qs(p.query).get("id", [""])[0]
        if not account.isdigit():
            raise ValueError("Facebook 页面缺少稳定 ID")
        return f"https://www.facebook.com/profile.php?id={account}"
    if p.query:
        raise ValueError("目标链接不应包含额外参数")
    path = p.path.rstrip("/")
    if platform == "youtube":
        path = path.removesuffix("/community")
    return p._replace(netloc=spec.host, path=path + "/", query="", fragment="").geturl()


def validate(platform, publication, media):
    spec = SPECS[platform]
    text = publication.get("body", "")
    if not text.strip() or len(text) > spec.text_limit:
        raise ValueError(f"内容长度超出 {platform} 当前限制")
    if spec.title_required and (
        not publication.get("title") or len(publication["title"]) > 100
    ):
        raise ValueError("此平台需要 1–100 字的标题")
    kinds = {m["metadata"]["kind"] for m in media}
    if len(kinds) > 1:
        raise ValueError("当前发布不混用图片和视频")
    kind = next(iter(kinds), "text")
    if kind not in spec.formats:
        raise ValueError("此平台当前发布类型需要" + ("/".join(spec.formats)))
    if platform == "telegram" and kind != "text" and len(text) > 1000:
        raise ValueError("媒体说明当前最多 1000 字")
    if kind == "image" and (
        len(media) > spec.image_count
        or any(m["size_bytes"] > spec.image_bytes for m in media)
    ):
        raise ValueError("图片数量或大小超出当前平台限制")
    if kind == "video":
        if len(media) != 1:
            raise ValueError("每次发布只选择一个视频")
        if media[0]["metadata"].get("duration", 0) > spec.video_seconds:
            raise ValueError("视频时长超出当前平台限制")
    if (
        platform == "youtube"
        and kind == "video"
        and publication.get("made_for_kids") not in (True, False)
    ):
        raise ValueError("YouTube 视频需要确认是否面向儿童")
    if platform == "tiktok" and publication.get("commercial_disclosure") != "own_brand":
        raise ValueError("TikTok 推广内容需要确认商业内容声明")
    if (
        platform in ("tiktok", "youtube")
        and kind == "video"
        and any(m["rights"].get("ai_generated") is None for m in media)
    ):
        raise ValueError("需要先确认视频是否包含需要披露的 AI 生成内容")
    return kind
