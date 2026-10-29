import io

import av
import pytest
from fastapi.testclient import TestClient
from lead_app import assets, domain
from lead_app.api import app
from lead_app.db import uid
from lead_app.security import token
from PIL import Image
from starlette.datastructures import UploadFile


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    yield aid
    domain.set_control(aid, "archive")


def picture():
    buffer = io.BytesIO()
    img = Image.new("RGB", (96, 64), (20, 100, 70))
    exif = Image.Exif()
    exif[315] = "private-test-author"
    img.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def video(tmp_path):
    path = tmp_path / "source.mp4"
    with av.open(str(path), "w") as container:
        container.metadata["title"] = "private-test-metadata"
        stream = container.add_stream("libx264", rate=24)
        stream.width = 64
        stream.height = 64
        stream.pix_fmt = "yuv420p"
        for i in range(12):
            frame = av.VideoFrame.from_image(
                Image.new("RGB", (64, 64), (i * 15, 90, 60))
            )
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path.read_bytes()


@pytest.mark.asyncio
async def test_real_image_decode_metadata_removal_dedupe_and_scope(agent):
    blob = picture()
    first = await assets.upload(
        agent, UploadFile(file=io.BytesIO(blob), filename="../../danger.jpg")
    )
    same = await assets.upload(
        agent, UploadFile(file=io.BytesIO(blob), filename="another.jpg")
    )
    assert same["id"] == first["id"] and len(assets.listing(agent)) == 1
    record, path = assets.resolve(agent, str(first["id"]), first["sha256"])
    assert path.parent.name == agent and path.name != "danger.jpg"
    assert record["metadata"]["width"] == 96 and record["metadata"]["height"] == 64
    with Image.open(path) as decoded:
        assert not decoded.getexif()
    other = domain.create_agent()
    try:
        assert assets.listing(other) == []
        with pytest.raises(LookupError):
            assets.resolve(other, str(first["id"]))
        with pytest.raises(LookupError):
            domain.submit_message(other, "使用这份素材", uid(), [str(first["id"])])
    finally:
        domain.set_control(other, "archive")
    message_key = uid()
    m = domain.submit_message(agent, "使用这张图片", message_key, [str(first["id"])])
    assert (
        domain.submit_message(agent, "使用这张图片", message_key, [str(first["id"])])[
            "id"
        ]
        == m["id"]
    )
    with pytest.raises(ValueError):
        domain.submit_message(agent, "使用这张图片", message_key, [])
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        assets.resolve(agent, str(first["id"]))


@pytest.mark.asyncio
async def test_real_video_decode_and_metadata_strip(agent, tmp_path):
    item = await assets.upload(
        agent,
        UploadFile(file=io.BytesIO(video(tmp_path)), filename="video.mp4"),
        {"ai_generated": False, "source": "owner"},
    )
    assert item["metadata"]["kind"] == "video" and item["metadata"]["codec"] == "h264"
    assert 0 < item["metadata"]["duration"] < 1
    row, path = assets.resolve(agent, str(item["id"]))
    with av.open(str(path)) as container:
        assert "title" not in container.metadata
        assert next(container.decode(video=0)).width == 64


@pytest.mark.asyncio
async def test_malformed_and_active_content_rejected_without_orphans(agent):
    for data in [
        b"\x89PNG\r\n\x1a\nnot-an-image",
        b'<svg onload="fetch(1)"></svg>',
        b"#EXTM3U\nhttp://127.0.0.1/secret",
    ]:
        with pytest.raises(ValueError):
            await assets.upload(
                agent, UploadFile(file=io.BytesIO(data), filename="media.mp4")
            )
    assert not assets.listing(agent)
    from lead_app.config import STATE

    assert not list((STATE / "assets" / agent).glob("*"))


def test_media_http_auth_body_limit_and_cross_agent_access(agent):
    client = TestClient(app)
    assert (
        client.post(
            "/api/agents/" + agent + "/assets",
            files={"file": ("x.jpg", picture(), "image/jpeg")},
        ).status_code
        == 403
    )
    client.cookies.set("lead_session", token("owner"))
    headers = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}
    result = client.post(
        "/api/agents/" + agent + "/assets",
        headers=headers,
        files={"file": ("x.jpg", picture(), "image/jpeg")},
    )
    assert result.status_code == 200, result.text
    item = result.json()
    assert "object_ref" not in item
    assert (
        client.get("/api/agents/" + agent + "/assets/" + item["id"]).headers[
            "content-type"
        ]
        == "image/jpeg"
    )
    other = domain.create_agent()
    try:
        assert (
            client.get("/api/agents/" + other + "/assets/" + item["id"]).status_code
            == 404
        )
    finally:
        domain.set_control(other, "archive")
    too_big = client.post(
        "/api/agents/" + agent + "/assets",
        headers={**headers, "Content-Length": str(200 * 1024**2)},
        content=b"",
    )
    assert too_big.status_code == 413
