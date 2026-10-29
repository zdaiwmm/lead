"""Bounded media decoding in a child process; no network demuxers or user paths."""

import json
import sys
from pathlib import Path

MAX_IMAGE_PIXELS = 40_000_000
MAX_VIDEO_PIXELS = 3840 * 2160


def inspect(source, destination):
    import av
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with source.open("rb") as file:
        head = file.read(32)
    if (
        head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
        or head[:4] == b"RIFF"
        and head[8:12] == b"WEBP"
    ):
        if source.stat().st_size > 20 * 1024**2:
            raise ValueError("IMAGE_TOO_LARGE")
        with Image.open(source) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS or min(img.size) < 32:
                raise ValueError("IMAGE_DIMENSIONS")
            if getattr(img, "n_frames", 1) != 1:
                raise ValueError("ANIMATED_IMAGE_NOT_SUPPORTED")
            img.load()
            clean = ImageOps.exif_transpose(img)
            alpha = "A" in clean.getbands()
            clean = clean.convert("RGBA" if alpha else "RGB")
            ext = ".png" if alpha else ".jpg"
            path = destination.with_suffix(ext)
            clean.save(
                path,
                format="PNG" if alpha else "JPEG",
                **({} if alpha else {"quality": 92}),
            )
            return {
                "kind": "image",
                "media_type": "image/png" if alpha else "image/jpeg",
                "width": clean.width,
                "height": clean.height,
                "duration": None,
                "extension": ext,
                "metadata_stripped": True,
            }
    if head[4:8] == b"ftyp":
        fmt = "mov"
        ext = ".mp4"
        mime = "video/mp4"
    elif head[:4] == b"\x1aE\xdf\xa3":
        fmt = "matroska"
        ext = ".webm"
        mime = "video/webm"
    else:
        raise ValueError("UNSUPPORTED_MEDIA")
    # Explicit demuxer prevents treating uploaded text/playlists as network URLs.
    with av.open(
        str(source),
        format=fmt,
        options={
            "protocol_whitelist": "file",
            "probesize": "5000000",
            "analyzeduration": "5000000",
        },
    ) as inp:
        videos = list(inp.streams.video)
        if len(videos) != 1:
            raise ValueError("VIDEO_STREAM_REQUIRED")
        v = videos[0]
        if v.codec_context.name not in ("h264", "hevc", "vp8", "vp9", "av1"):
            raise ValueError("VIDEO_CODEC")
        if not v.width or not v.height or v.width * v.height > MAX_VIDEO_PIXELS:
            raise ValueError("VIDEO_DIMENSIONS")
        duration = (
            float(inp.duration / av.time_base)
            if inp.duration
            else float(v.duration * v.time_base)
            if v.duration
            else None
        )
        if not duration or not 0 < duration <= 600:
            raise ValueError("VIDEO_DURATION")
        rate = float(v.average_rate or 0)
        if not 0 < rate <= 120:
            raise ValueError("VIDEO_FRAME_RATE")
        next(inp.decode(v))  # Verify that the stream actually decodes.
        codec = v.codec_context.name
    with av.open(
        str(source), format=fmt, options={"protocol_whitelist": "file"}
    ) as inp:
        with av.open(
            str(destination.with_suffix(ext)),
            mode="w",
            format="mp4" if ext == ".mp4" else "webm",
            options={"movflags": "faststart"} if ext == ".mp4" else {},
        ) as out:
            streams = {
                s.index: out.add_stream_from_template(s)
                for s in list(inp.streams.video) + list(inp.streams.audio)[:1]
            }
            for packet in inp.demux():
                if packet.dts is None or packet.stream.index not in streams:
                    continue
                packet.stream = streams[packet.stream.index]
                out.mux(packet)
    return {
        "kind": "video",
        "media_type": mime,
        "width": v.width,
        "height": v.height,
        "duration": duration,
        "frame_rate": rate,
        "codec": codec,
        "extension": ext,
        "metadata_stripped": True,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(inspect(Path(sys.argv[1]), Path(sys.argv[2]))))
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
