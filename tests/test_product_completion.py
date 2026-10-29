import io
from decimal import Decimal
from uuid import uuid4

import pytest
from lead_app import assets, budget, domain, metering
from lead_app.db import one, tx
from PIL import Image
from starlette.datastructures import UploadFile


@pytest.mark.asyncio
async def test_owner_media_context_is_scoped_sourced_and_idempotent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid, other = domain.create_agent(), domain.create_agent()
    data = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(data, "PNG")
    item = await assets.upload(
        aid, UploadFile(file=io.BytesIO(data.getvalue()), filename="media.png")
    )
    key = str(uuid4())
    before = domain.agent_detail(aid)["config_revision"]
    msg = domain.submit_message(aid, "这是我们实拍的中文书封面", key, [str(item["id"])])
    again = domain.submit_message(
        aid, "这是我们实拍的中文书封面", key, [str(item["id"])]
    )
    assert msg["id"] == again["id"]
    with tx(aid) as c:
        saved = one(
            c, "SELECT metadata,rights FROM lead.assets WHERE id=%s", (item["id"],)
        )
        assert saved["metadata"]["owner_context"] == {
            "text": "这是我们实拍的中文书封面",
            "message_id": str(msg["id"]),
            "source": "owner_message",
        }
        assert (
            saved["rights"].get("ai_generated") is None
        )  # owner text isn't a model declaration
        assert (
            one(c, "SELECT config_revision FROM lead.agents")["config_revision"]
            == before + 1
        )
    with pytest.raises(LookupError):
        domain.submit_message(other, "cross scope", str(uuid4()), [str(item["id"])])


def test_usage_records_actual_provider_units_without_guessing_cost(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(metering, "STATE", tmp_path)
    aid = domain.create_agent()
    reservation = budget.reserve(aid, str(uuid4()), 1000)
    details = {
        "model": "fixture",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "cache_write_tokens": 0,
        "unmeasured_requests": 0,
        "requests": 1,
        "cache_breakdown_verified": True,
    }
    budget.settle(aid, reservation, actual=120, details=details)
    budget.settle(aid, reservation, actual=120, details=details)
    measured = metering.summary(aid)
    assert measured["model_tokens"] == 120 and measured["cached_input_tokens"] == 30
    assert measured["model_costs"] == {} and measured["unpriced_calls"] == 1
    import json

    (tmp_path / "operator-rates.json").write_text(
        json.dumps(
            {
                "version": "fixture-v1",
                "effective_at": "2026-10-29",
                "currency": "CNY",
                "models": {
                    "fixture": {
                        "input_per_million": 2,
                        "cached_input_per_million": 1,
                        "output_per_million": 3,
                    }
                },
            }
        )
    )
    rated = metering.priced(details)
    assert Decimal(rated["cost"]) == Decimal("0.00023")
    assert metering.priced({**details, "cache_read_tokens": 101})["cost"] is None
    assert metering.priced({**details, "unmeasured_requests": 1})["cost"] is None


def test_export_and_explicit_purge_never_select_other_agents(monkeypatch):
    import json
    import zipfile

    import psycopg
    from lead_app import lifecycle
    from lead_app.config import TENANT, database_kwargs

    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid, other = domain.create_agent(), domain.create_agent()
    domain.submit_message(aid, "owned business fact", str(uuid4()))
    domain.submit_message(other, "must remain private", str(uuid4()))
    package = lifecycle.export_archive(aid)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        value = json.loads(archive.read("lead.json"))
    assert "owned business fact" in str(value) and "must remain private" not in str(
        value
    )
    with pytest.raises(ValueError, match="归档"):
        lifecycle.purge(TENANT, aid)
    domain.set_control(aid, "archive")
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "UPDATE lead.agents SET updated_at=now()-interval '8 days' WHERE id=%s",
            (aid,),
        )
    preview = lifecycle.purge(TENANT, aid)
    assert preview["state"] == "preview"
    assert lifecycle.purge(TENANT, aid, apply=True)["state"] == "purged"
    assert (
        domain.agent_detail(other)["messages"][-1]["content"]["text"]
        == "must remain private"
    )


@pytest.mark.asyncio
async def test_platform_environment_wait_does_not_require_user_authorization(
    monkeypatch,
):
    import httpx
    from lead_app import activities

    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    domain.connect_lab(aid)
    task = domain.queue_lab_publish(
        aid, "Infrastructure recovery fixture", str(uuid4())
    )

    async def unavailable(*a, **k):
        request = httpx.Request("POST", "http://127.0.0.1:8089/fixture")
        response = httpx.Response(
            409, request=request, json={"code": "browser_busy", "detail": "出口维护中"}
        )
        response.raise_for_status()

    monkeypatch.setattr(activities, "browser_rpc", unavailable)
    with pytest.raises(RuntimeError, match="browser_not_dispatched"):
        await activities.execute_task({"agent_id": aid, "task_id": str(task["id"])})
    with tx(aid) as c:
        assert one(c, "SELECT state FROM lead.tasks")["state"] == "running"
        assert (
            one(c, "SELECT health_state FROM lead.agents")["health_state"] == "queued"
        )
        assert one(c, "SELECT state FROM lead.actions")["state"] == "prepared"
        assert one(c, "SELECT count(*) AS n FROM lead.lab_posts")["n"] == 0


@pytest.mark.asyncio
async def test_large_export_has_explicit_scoped_parts(monkeypatch):
    import json
    import zipfile

    from lead_app import lifecycle

    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    sizes = []
    for color in ("red", "green", "blue"):
        data = io.BytesIO()
        Image.new("RGB", (32, 32), color).save(data, "PNG")
        item = await assets.upload(
            aid, UploadFile(file=io.BytesIO(data.getvalue()), filename=color + ".png")
        )
        sizes.append(item["size_bytes"])
    monkeypatch.setattr(lifecycle, "MAX_EXPORT_BYTES", max(sizes))
    with pytest.raises(ValueError, match="分批"):
        lifecycle.export_archive(aid)
    for part in (1, 2, 3):
        with zipfile.ZipFile(
            io.BytesIO(lifecycle.export_archive(aid, part))
        ) as archive:
            assert len(archive.namelist()) == 2
            manifest = json.loads(archive.read("lead.json"))
            assert manifest["export_part"] == part and manifest["export_parts"] == 3
    with pytest.raises(ValueError, match="不存在"):
        lifecycle.export_archive(aid, 4)
