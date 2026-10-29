import pytest
from fastapi.testclient import TestClient
from lead_app import domain
from lead_app.activities import prepare_action
from lead_app.api import app
from lead_app.commands import apply_asset_annotations, apply_preference
from lead_app.db import one, tx, uid
from lead_app.events import revision
from lead_app.model import AssetAnnotation, Decision
from lead_app.security import token


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    yield aid
    domain.set_control(aid, "archive")


def test_preference_commands_are_versioned_idempotent_and_reversible(agent):
    message = domain.submit_message(agent, "请记住以后只用英文", uid())
    with tx(agent) as c:
        first = apply_preference(
            c, agent, message, Decision(reply="ok", preference="只用英文"), 1
        )
        assert "已保存" in first
        assert (
            apply_preference(
                c, agent, message, Decision(reply="ok", preference="只用英文"), 1
            )
            == first
        )
        memory = one(c, "SELECT id FROM lead.memory_entries WHERE deleted_at IS NULL")
        assert one(c, "SELECT count(*) AS n FROM lead.conversation_commands")["n"] == 1
        assert one(c, "SELECT config_revision FROM lead.agents")["config_revision"] == 2
    change = domain.submit_message(agent, "改为只用中文，替换之前的语言偏好", uid())
    with tx(agent) as c:
        apply_preference(
            c,
            agent,
            change,
            Decision(
                reply="ok",
                preference="只用中文",
                preference_mode="replace",
                preference_ids=[str(memory["id"])],
            ),
            2,
        )
        assert one(
            c, "SELECT deleted_at FROM lead.memory_entries WHERE id=%s", (memory["id"],)
        )["deleted_at"]
        live = one(
            c, "SELECT id,body FROM lead.memory_entries WHERE deleted_at IS NULL"
        )
        assert live["body"]["text"] == "只用中文"
    before = revision()
    result = domain.forget_preference(agent, str(live["id"]), 3)
    assert result["revision"] == 4 and before != revision()
    assert domain.forget_preference(agent, str(live["id"]), 3)["duplicate"]


def test_hypothetical_or_stale_preference_cannot_apply(agent):
    message = domain.submit_message(agent, "如果以后改为只用英文会怎样？", uid())
    with tx(agent) as c:
        reply = apply_preference(
            c, agent, message, Decision(reply="ok", preference="只用英文"), 1
        )
        assert "尚未改变" in reply
        assert not one(c, "SELECT id FROM lead.memory_entries")
    actual = domain.submit_message(agent, "请记住语气克制", uid())
    with tx(agent) as c:
        domain.remember(c, agent, "先前已更新")
        reply = apply_preference(
            c, agent, actual, Decision(reply="ok", preference="语气克制"), 1
        )
        assert "尚未执行" in reply
        assert one(c, "SELECT count(*) AS n FROM lead.memory_entries")["n"] == 1


def test_preference_update_cancels_stale_queued_content(agent):
    domain.connect_lab(agent)
    task = domain.queue_lab_publish(agent, "旧方向下的内容", "stale-preference")
    with tx(agent) as c:
        domain.remember(c, agent, "改为新的方向")
    assert prepare_action(agent, str(task["id"])) is None
    with tx(agent) as c:
        assert not one(c, "SELECT id FROM lead.actions")


def test_scoped_preference_removal_and_sse_auth(agent):
    with tx(agent) as c:
        memory = domain.remember(c, agent, "语气克制")
    other = domain.create_agent()
    try:
        with pytest.raises(LookupError):
            domain.forget_preference(other, memory, 1)
        client = TestClient(app)
        assert client.get("/api/events").status_code == 401
        client.cookies.set("lead_session", token("owner"))
        headers = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}
        wrong = client.post(
            f"/api/agents/{agent}/memory/{memory}/remove",
            headers=headers,
            json={"expected_revision": 1},
        )
        assert wrong.status_code == 409
        assert (
            client.post(
                f"/api/agents/{agent}/memory/{memory}/remove",
                headers=headers,
                json={"expected_revision": 2},
            ).status_code
            == 200
        )
    finally:
        domain.set_control(other, "archive")


@pytest.mark.asyncio
async def test_asset_declaration_is_explicit_scoped_and_idempotent(agent):
    import io

    from lead_app import assets
    from PIL import Image
    from starlette.datastructures import UploadFile

    data = io.BytesIO()
    Image.new("RGB", (64, 64), "green").save(data, "PNG")
    asset = await assets.upload(
        agent, UploadFile(file=io.BytesIO(data.getvalue()), filename="photo.png")
    )
    annotation = AssetAnnotation(id=asset["id"], ai_generated=False)
    question = domain.submit_message(agent, "这张图片是否属于 AI 生成？", uid())
    with tx(agent) as c:
        assert "尚未改变" in apply_asset_annotations(c, agent, question, [annotation])
        assert (
            one(c, "SELECT rights FROM lead.assets")["rights"]["ai_generated"] is None
        )
    message = domain.submit_message(agent, "这张图片是我自己拍的实拍", uid())
    with tx(agent) as c:
        first = apply_asset_annotations(c, agent, message, [annotation])
        assert apply_asset_annotations(c, agent, message, [annotation]) == first
        assert one(c, "SELECT config_revision FROM lead.agents")["config_revision"] == 2
        assert (
            one(c, "SELECT rights FROM lead.assets")["rights"]["ai_generated"] is False
        )
    other = domain.create_agent()
    try:
        msg = domain.submit_message(other, "这张素材是我自己拍的", uid())
        with pytest.raises(LookupError), tx(other) as c:
            apply_asset_annotations(c, other, msg, [annotation])
    finally:
        domain.set_control(other, "archive")


def test_low_disk_stops_new_writes_and_releases_reservations(monkeypatch):
    from types import SimpleNamespace

    from lead_app import resources

    monkeypatch.setattr(
        resources.shutil, "disk_usage", lambda path: SimpleNamespace(free=32 * 1024**2)
    )
    with pytest.raises(ValueError):
        resources.ensure_capacity(64)
    with pytest.raises(ValueError), resources.reserve_disk(256 * 1024**2):
        pass
    assert resources._reserved == 0
    monkeypatch.setattr(
        resources.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024**3)
    )
    with pytest.raises(RuntimeError), resources.reserve_disk(256 * 1024**2):
        assert resources._reserved == 256 * 1024**2
        raise RuntimeError("aborted upload")
    assert resources._reserved == 0
