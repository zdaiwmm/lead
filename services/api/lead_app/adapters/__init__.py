from .catalog import SPECS
from .web import WebAdapter


def get_adapter(platform):
    if platform == "telegram":
        from .telegram_media import TelegramMediaAdapter

        return TelegramMediaAdapter()
    if platform not in SPECS:
        raise ValueError("此平台适配器不存在")
    return WebAdapter(platform)


def target_in_message(text):
    import re

    from .catalog import target_url

    candidates = re.findall(
        r"https://[^\s<>]+|(?:t\.me|telegram\.me)/[A-Za-z][A-Za-z0-9_]{3,31}",
        text,
        re.I,
    )
    for value in candidates:
        value = value.rstrip("。.,，!！)）]")
        if not value.lower().startswith("https://"):
            value = "https://" + value
        for platform in SPECS:
            try:
                return platform, target_url(platform, value)
            except ValueError:
                pass
    return None
