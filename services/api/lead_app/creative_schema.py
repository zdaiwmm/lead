"""Text-only inputs for the local, deterministic creative renderer."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CreativeCard(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    heading: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=360)


class CreativeBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["image", "video"] = "image"
    headline: str = Field(min_length=1, max_length=120)
    cards: list[CreativeCard] = Field(min_length=1, max_length=6)
    cta: str = Field(min_length=1, max_length=140)
    style: Literal["steps", "question", "contrast"] = "steps"
    # This renderer creates diagrams/text explainers, never product footage.
    presentation: Literal["explainer"] = "explainer"


PROFILES = {
    "facebook": {"image": (1200, 900), "video": (720, 1280)},
    "instagram": {"image": (1080, 1350), "video": (720, 1280)},
    "youtube": {"image": (1280, 720), "video": (1280, 720)},
    "tiktok": {"video": (720, 1280)},
    "x": {"image": (1200, 675), "video": (1280, 720)},
    "linkedin": {"image": (1200, 1200), "video": (1280, 720)},
    "reddit": {"image": (1200, 900), "video": (1280, 720)},
    "pinterest": {"image": (1000, 1500), "video": (720, 1280)},
    "threads": {"image": (1080, 1350), "video": (720, 1280)},
    "whatsapp": {"image": (1080, 1080), "video": (720, 1280)},
}

RENDERER_VERSION = "editorial-explainer-2"
VIDEO_FPS = 24

COVER_SIZES = {
    "facebook": (1640, 624),
    "youtube": (2560, 1440),
    "x": (1500, 500),
    "linkedin": (1584, 396),
    "reddit": (1920, 384),
}


def fallback_brief(body, title, kind, topic="", style="steps"):
    """Reuse the planner's actual copy; do not invent demonstration results."""
    import re

    parts = [
        p.strip() for p in re.split(r"\n\s*\n|(?<=[。！？.!?])\s+", body) if p.strip()
    ]
    if not parts:
        raise ValueError("没有可制作的正文")
    cards = []
    for part in parts:
        while part:
            cards.append(
                CreativeCard(heading=topic or title or "Guide", body=part[:360])
            )
            part = part[360:]
    if len(cards) > 6:
        raise ValueError("内容过长，需要先提炼媒体文案")
    return CreativeBrief(
        kind=kind,
        headline=(title or topic or "A practical guide")[:120],
        cards=cards,
        cta="查看产品当前选项 / Explore the current options",
        style=style,
    )
