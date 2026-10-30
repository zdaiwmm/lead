"""Generate and decode all channel media locally, without accounts or model calls."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))

import av
from lead_app.config import STATE
from lead_app.creative_schema import (
    PROFILES,
    RENDERER_VERSION,
    VIDEO_FPS,
    CreativeBrief,
    CreativeCard,
)
from lead_app.creative_worker import font_path, render
from PIL import Image


def verify():
    folder = STATE / "verification" / "creative-autonomy"
    folder.mkdir(parents=True, exist_ok=True)
    font_path("中文")
    font_path("English")
    brief = CreativeBrief(
        headline="Keep key terms consistent",
        cards=[
            CreativeCard(
                heading="Prepare", body="List the names and terms you want to review."
            ),
            CreativeCard(
                heading="Configure",
                body="Check your language, glossary and style settings.",
            ),
            CreativeCard(
                heading="Review",
                body="Use a sample you are allowed to process. Check the result.",
            ),
        ],
        cta="Explore the current options at seka.one",
    )
    checks = []
    for platform, formats in PROFILES.items():
        for kind in formats:
            target = folder / f"{platform}-{kind}"
            target.mkdir(exist_ok=True)
            payload = {
                "brand": "Seka",
                "display_url": "seka.one/translate",
                "platform": platform,
                "brief": {**brief.model_dump(), "kind": kind},
            }
            result = render(payload, target)
            file = target / result["filename"]
            if kind == "video":
                frames = 0
                with av.open(str(file)) as container:
                    for frame in container.decode(video=0):
                        assert (frame.width, frame.height) == PROFILES[platform][kind]
                        frames += 1
                assert frames == round(result["duration"] * VIDEO_FPS)
            else:
                with Image.open(file) as image:
                    image.load()
                    assert image.size == PROFILES[platform][kind]
            checks.append(
                {
                    "platform": platform,
                    "kind": kind,
                    "decoded": True,
                    "width": result["width"],
                    "height": result["height"],
                    "duration": result["duration"],
                    "bytes": file.stat().st_size,
                }
            )
    report = {
        "ok": True,
        "renderer": RENDERER_VERSION,
        "accounts_used": 0,
        "external_publications": 0,
        "checks": checks,
    }
    (folder / "report.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "ok": True,
                "media_formats": len(checks),
                "report": str(folder / "report.json"),
            }
        )
    )


if __name__ == "__main__":
    verify()
