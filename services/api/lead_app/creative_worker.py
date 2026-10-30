"""Offline typography, poster and captioned H.264 production in a child process.

No URLs, browser, shell commands, uploaded files or generated code are evaluated.
All artwork is original shapes/type. Product footage is never fabricated.
"""

import json
import math
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .creative_schema import (
    COVER_SIZES,
    PROFILES,
    RENDERER_VERSION,
    VIDEO_FPS,
    CreativeBrief,
)

PAPER = "#F4F1E8"
INK = "#192C2A"
MUTED = "#4B625D"
ACCENT = "#C74625"
GREEN = "#D4E5D9"


def font_path(text=""):
    cjk = bool(re.search(r"[\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    names = (
        [
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        ]
        if cjk
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
    )
    # An operator can install a font; the model cannot supply filesystem paths.
    configured = os.environ.get("LEAD_CREATIVE_FONT")
    if configured:
        names.insert(0, configured)
    for name in names:
        if Path(name).is_file():
            return name
    raise ValueError("CREATIVE_CJK_FONT_REQUIRED" if cjk else "CREATIVE_FONT_REQUIRED")


def wrap(text, font, width):
    """Preserve words when possible, split CJK/long words without clipping."""
    lines = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for token in re.findall(
            r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*|[^A-Za-z0-9]", paragraph
        ):
            candidate = line + token
            if font.getlength(candidate) <= width:
                line = candidate
                continue
            if line.strip():
                lines.append(line.rstrip())
                line = ""
            for char in token.lstrip():
                if line and font.getlength(line + char) > width:
                    lines.append(line)
                    line = ""
                line += char
        if line.strip():
            lines.append(line.rstrip())
    return lines or [""]


def text_box(draw, text, box, size, *, color=INK, minimum=22):
    x, y, width, height = box
    path = font_path(text)
    for points in range(size, minimum - 1, -2):
        font = ImageFont.truetype(path, points)
        lines = wrap(text, font, width)
        spacing = round(points * 1.38)
        if len(lines) * spacing <= height:
            for n, line in enumerate(lines):
                draw.text(
                    (x, y + n * spacing), line, font=font, fill=color, anchor="lt"
                )
            return {"font_size": points, "lines": len(lines), "fits": True}
    raise ValueError("CREATIVE_TEXT_OVERFLOW")


def canvas(size, brand, url, label):
    width, height = size
    image = Image.new("RGB", size, PAPER)
    draw = ImageDraw.Draw(image)
    margin = round(width * 0.075)
    draw.rectangle((0, 0, 15, height), fill=ACCENT)
    text_box(draw, brand, (margin, margin, width * 0.60, 65), 38, minimum=22)
    text_box(
        draw,
        label,
        (margin, margin + 70, width - 2 * margin, 40),
        22,
        color=MUTED,
        minimum=18,
    )
    draw.line((margin, height - 105, width - margin, height - 105), fill=MUTED, width=1)
    text_box(
        draw,
        url[:90],
        (margin, height - 78, width - margin * 2, 27),
        20,
        color=MUTED,
        minimum=16,
    )
    return image


def poster(brief, size, brand, url):
    width, height = size
    image = canvas(size, brand, url, "PRACTICAL GUIDE  /  AI-ASSISTED EXPLAINER")
    draw = ImageDraw.Draw(image)
    margin = round(width * 0.075)
    if width / height > 1.2:
        text_box(
            draw, brief.headline, (margin, 211, width - 2 * margin, 87), 46, minimum=28
        )
        columns = min(3, len(brief.cards))
        rows = math.ceil(len(brief.cards) / columns)
        gap = 14
        cell_width = (width - 2 * margin - gap * (columns - 1)) / columns
        cell_height = (height - 450 - gap * (rows - 1)) / rows
        for index, card in enumerate(brief.cards):
            x = margin + (index % columns) * (cell_width + gap)
            y = 310 + (index // columns) * (cell_height + gap)
            draw.rounded_rectangle(
                (x, y, x + cell_width, y + cell_height), radius=12, fill=GREEN
            )
            label = (
                "?"
                if brief.style == "question"
                else "→"
                if brief.style == "contrast"
                else f"{index + 1:02}"
            )
            text_box(
                draw,
                f"{label}  {card.heading}\n{card.body}",
                (x + 18, y + 18, cell_width - 36, cell_height - 36),
                26,
                minimum=20,
            )
        text_box(
            draw,
            brief.cta,
            (margin, height - 135, width - 2 * margin, 28),
            22,
            color=ACCENT,
            minimum=16,
        )
        return image
    top = margin + 138
    headline_height = round(height * 0.19)
    text_box(
        draw,
        brief.headline,
        (margin, top, width - 2 * margin, headline_height),
        64,
        minimum=30,
    )
    top += headline_height + 12
    available = height - 215 - top
    # All source cards are represented; overflow fails rather than hiding text.
    gap = 12
    row_height = (available - gap * (len(brief.cards) - 1)) / len(brief.cards)
    for index, card in enumerate(brief.cards):
        y = round(top + index * (row_height + gap))
        draw.rounded_rectangle(
            (margin, y, width - margin, y + row_height), radius=12, fill=GREEN
        )
        label = (
            "?"
            if brief.style == "question"
            else "→"
            if brief.style == "contrast"
            else f"{index + 1:02}"
        )
        text_box(
            draw,
            label,
            (margin + 18, y + 15, 60, row_height - 22),
            27,
            color=ACCENT,
            minimum=18,
        )
        content = card.heading + "\n" + card.body
        text_box(
            draw,
            content,
            (margin + 90, y + 15, width - 2 * margin - 110, row_height - 28),
            30,
            minimum=20,
        )
    text_box(
        draw,
        brief.cta,
        (margin, height - 196, width - 2 * margin, 70),
        27,
        color=ACCENT,
        minimum=18,
    )
    return image


def video_slide(brief, card, size, brand, url, number, total):
    width, height = size
    image = canvas(size, brand, url, f"EXPLAINER  /  {number:02} OF {total:02}")
    draw = ImageDraw.Draw(image)
    margin = round(width * 0.09)
    if width > height:
        text_box(
            draw,
            brief.headline,
            (margin, 212, width - 2 * margin, 48),
            26,
            color=MUTED,
            minimum=18,
        )
        text_box(
            draw, card.heading, (margin, 270, width - 2 * margin, 85), 48, minimum=28
        )
        text_box(
            draw,
            card.body,
            (margin, 365, width - 2 * margin, height - 550),
            32,
            minimum=24,
        )
        if number == total:
            text_box(
                draw,
                brief.cta,
                (margin, height - 183, width - 2 * margin, 60),
                26,
                color=ACCENT,
                minimum=20,
            )
        return image
    top = round(height * 0.24)
    text_box(
        draw,
        brief.headline,
        (margin, top - 75, width - 2 * margin, 58),
        26,
        color=MUTED,
        minimum=18,
    )
    draw.ellipse(
        (width - margin - 72, margin, width - margin, margin + 72), fill=ACCENT
    )
    text_box(
        draw,
        str(number),
        (width - margin - 52, margin + 13, 42, 48),
        38,
        color=PAPER,
        minimum=22,
    )
    text_box(
        draw,
        card.heading,
        (margin, top, width - 2 * margin, height * 0.20),
        62,
        minimum=30,
    )
    text_box(
        draw,
        card.body,
        (margin, top + height * 0.22, width - 2 * margin, height * 0.30),
        38,
        minimum=24,
    )
    if number == total:
        text_box(
            draw,
            brief.cta,
            (margin, height - 198, width - 2 * margin, 70),
            30,
            color=ACCENT,
            minimum=22,
        )
    return image


def cue_time(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def brand_art(size, brand, url, avatar=False):
    width, height = size
    image = Image.new("RGB", size, INK)
    draw = ImageDraw.Draw(image)
    if avatar:
        draw.ellipse(
            (width * 0.16, height * 0.16, width * 0.84, height * 0.84), fill=GREEN
        )
        words = re.findall(r"[A-Za-z]+", brand)
        initials = "".join(w[0] for w in words[:2]).upper() if words else brand[:2]
        font = ImageFont.truetype(
            font_path(initials), 260 if len(initials) <= 2 else 160
        )
        draw.text(
            (width / 2, height / 2), initials or "A", anchor="mm", font=font, fill=INK
        )
    else:
        # Place copy in the middle safe band, including YouTube's broad canvas.
        safe_width = min(width - 160, 1250)
        x = (width - safe_width) / 2
        y = height / 2 - 100
        draw.rectangle((x - 30, y, x - 18, y + 180), fill=ACCENT)
        text_box(draw, brand, (x, y, safe_width, 105), 70, color=PAPER, minimum=30)
        text_box(
            draw, url[:90], (x, y + 116, safe_width, 65), 28, color=GREEN, minimum=20
        )
    return image


def render(payload, destination):
    brief = CreativeBrief.model_validate(payload["brief"])
    platform = payload["platform"]
    purpose = payload.get("purpose", "publication")
    size = (
        (1024, 1024)
        if purpose == "profile_avatar"
        else COVER_SIZES[platform]
        if purpose == "profile_cover"
        else PROFILES[platform][brief.kind]
    )
    brand = payload["brand"][:80]
    url = payload["display_url"]
    captions = []
    if brief.kind == "image":
        path = destination / "poster.png"
        artwork = (
            brand_art(size, brand, url, purpose == "profile_avatar")
            if purpose in {"profile_avatar", "profile_cover"}
            else poster(brief, size, brand, url)
        )
        artwork.save(path, "PNG")
        duration = None
    else:
        import av

        path = destination / "explainer.mp4"
        elapsed = 0
        frame_index = 0
        rate = VIDEO_FPS
        with av.open(
            str(path), "w", format="mp4", options={"movflags": "faststart"}
        ) as container:
            stream = container.add_stream("libx264", rate=rate)
            stream.width, stream.height = size
            stream.pix_fmt = "yuv420p"
            stream.options = {"preset": "ultrafast", "crf": "24", "threads": "2"}
            for number, card in enumerate(brief.cards, 1):
                seconds = min(
                    16, max(5, math.ceil((len(card.heading) + len(card.body)) / 13))
                )
                image = video_slide(
                    brief, card, size, brand, url, number, len(brief.cards)
                )
                captions.append(
                    {
                        "start": elapsed,
                        "end": elapsed + seconds,
                        "text": card.heading + "\n" + card.body,
                    }
                )
                for n in range(seconds * rate):
                    frame_image = image.copy()
                    draw = ImageDraw.Draw(frame_image)
                    draw.rectangle(
                        (0, size[1] - 7, size[0] * (n + 1) / (seconds * rate), size[1]),
                        fill=ACCENT,
                    )
                    frame = av.VideoFrame.from_image(frame_image)
                    frame.pts = frame_index
                    frame_index += 1
                    for packet in stream.encode(frame):
                        container.mux(packet)
                elapsed += seconds
            for packet in stream.encode():
                container.mux(packet)
        duration = elapsed
        # Read a real encoded frame for the preview, not an unrelated poster.
        with av.open(str(path)) as container:
            next(container.decode(video=0)).to_image().save(destination / "preview.png")
    return {
        "filename": path.name,
        "preview": "preview.png" if brief.kind == "video" else path.name,
        "renderer_version": RENDERER_VERSION,
        "width": size[0],
        "height": size[1],
        "duration": duration,
        "captions": captions,
        "vtt": "WEBVTT\n\n"
        + "\n\n".join(
            f"{cue_time(c['start'])} --> {cue_time(c['end'])}\n{c['text']}"
            for c in captions
        ),
        "quality": {
            "text_fits": True,
            "original_type_and_shapes": True,
            "presentation": "explainer",
            "audio": "none",
            "burned_in_text": True,
        },
    }


if __name__ == "__main__":
    try:
        request_path = Path(sys.argv[1])
        print(
            json.dumps(
                render(json.loads(request_path.read_text()), request_path.parent)
            )
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": str(error)
                    if isinstance(error, ValueError)
                    else type(error).__name__
                }
            )
        )
        sys.exit(1)
