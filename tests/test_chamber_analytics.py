import json
from datetime import datetime, time, timedelta
from functools import partial
from secrets import token_urlsafe
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from lead_app import chamber_analytics, distribution, domain
from lead_app.analytics_reporting import TZ, refresh_scope
from lead_app.collector_service import app
from lead_app.db import one, tx, uid
from lead_app.scope import current_tenant
from lead_app.security import token


def auth(key):
    return {"Authorization": "Bearer " + key}


@pytest.fixture
def production(growth_agent, monkeypatch):
    with tx(growth_agent) as c:
        connection = str(
            one(
                c,
                """INSERT INTO lead.analytics_connections
               (tenant_id,agent_id,provider,site_host,state,secret_ref,last_received_at)
               VALUES(lead.ctx_tenant(),%s,'lead_site','https://site.example','verified',
                      'hmac:client-write',now()) RETURNING id""",
                (growth_agent,),
            )["id"]
        )
    read_key, link_key = token_urlsafe(32), token_urlsafe(32)
    monkeypatch.setenv(chamber_analytics.SITE_CONNECTION, connection)
    monkeypatch.setenv(chamber_analytics.READ_KEY, read_key)
    monkeypatch.setenv(chamber_analytics.LINK_KEY, link_key)
    return growth_agent, connection, read_key, link_key


def test_unconfigured_production_routes_are_closed(monkeypatch):
    monkeypatch.delenv(chamber_analytics.SITE_CONNECTION, raising=False)
    with TestClient(app, base_url="https://collector.example") as client:
        assert (
            client.get(
                chamber_analytics.REPORT_PATH, params={"day": "2026-11-20"}
            ).status_code
            == 404
        )
        assert client.post(chamber_analytics.LINKS_PATH, json={}).status_code == 404
        assert client.get("/chamber/r/" + token_urlsafe(12)).status_code == 404


def test_production_registry_is_scoped_idempotent_and_keeps_existing_routes(production):
    aid, _, read_key, link_key = production
    payload = {
        "code": token_urlsafe(12),
        "platform": "github",
        "destination_url": "https://site.example/article?q=1",
    }
    day = datetime.now(TZ).date().isoformat()
    report = chamber_analytics.REPORT_PATH
    registry = chamber_analytics.LINKS_PATH
    with TestClient(app, base_url="https://collector.example") as client:
        assert (
            client.get(report, params={"day": day}, headers=auth(link_key)).status_code
            == 401
        )
        assert (
            client.post(registry, json=payload, headers=auth(read_key)).status_code
            == 401
        )
        assert (
            client.post(registry, json=payload, headers=auth(link_key)).status_code
            == 204
        )
        assert (
            client.post(registry, json=payload, headers=auth(link_key)).status_code
            == 204
        )
        assert (
            client.post(
                registry, json={**payload, "platform": "x"}, headers=auth(link_key)
            ).status_code
            == 409
        )
        assert (
            client.post(
                registry,
                json={**payload, "destination_url": "https://outside.example/"},
                headers=auth(link_key),
            ).status_code
            == 422
        )
        assert (
            client.post(
                registry,
                json={**payload, "destination_url": "https://site.example:444/"},
                headers=auth(link_key),
            ).status_code
            == 422
        )
        wrong_origin = token_urlsafe(12)
        with tx(aid) as c:
            ordinary = distribution.register(
                c,
                aid,
                "github",
                "https://site.example/",
                intent="test:ordinary",
                placement="manual",
                code=token_urlsafe(12),
            )
            distribution.register(
                c,
                aid,
                "github",
                "https://outside.example/",
                intent="chamber:" + wrong_origin,
                placement="chamber",
                code=wrong_origin,
            )
            existing = one(
                c,
                "SELECT platform,destination_url,intent_key FROM lead.tracking_links WHERE public_code=%s",
                (payload["code"],),
            )
        assert existing == {
            "platform": payload["platform"],
            "destination_url": payload["destination_url"],
            "intent_key": "chamber:" + payload["code"],
        }
        assert (
            client.get(
                "/chamber/r/" + ordinary["public_code"], follow_redirects=False
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/r/" + ordinary["public_code"], follow_redirects=False
            ).status_code
            == 302
        )
        assert (
            client.get("/chamber/r/" + wrong_origin, follow_redirects=False).status_code
            == 404
        )
        assert (
            client.get("/r/" + payload["code"], follow_redirects=False).status_code
            == 404
        )
        assert (
            client.post(
                registry,
                json={**payload, "code": ordinary["public_code"]},
                headers=auth(link_key),
            ).status_code
            == 409
        )
        other = domain.create_agent(runtime_engine="growth")
        foreign_code = token_urlsafe(12)
        with tx(other) as c:
            distribution.register(
                c,
                other,
                "github",
                "https://site.example/",
                intent="chamber:" + foreign_code,
                placement="chamber",
                code=foreign_code,
            )
        assert (
            client.get("/chamber/r/" + foreign_code, follow_redirects=False).status_code
            == 404
        )
        assert (
            client.post(
                registry, json={**payload, "code": foreign_code}, headers=auth(link_key)
            ).status_code
            == 409
        )
        response = client.get(report, params={"day": day}, headers=auth(read_key))
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"


def test_production_redirect_attributes_pv_and_deduplicates_both_device_counts(
    production, process_pending_clicks
):
    aid, connection, read_key, link_key = production
    codes = [token_urlsafe(12), token_urlsafe(12)]
    payload = {
        "platform": "github",
        "destination_url": "https://site.example/?id=1",
    }
    device = uid()
    day = datetime.now(TZ).date().isoformat()
    with TestClient(app, base_url="https://collector.example") as client:
        assert (
            all(client.post(
                chamber_analytics.LINKS_PATH, json={**payload, "code": code}, headers=auth(link_key)
            ).status_code == 204 for code in codes)
        )
        first_cookie = None
        for code in codes:
            click = client.get(
                "/chamber/r/" + code,
                follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            assert click.status_code == 302, click.text
            assert "Path=/chamber/r/" in click.headers["set-cookie"]
            assert "Secure" in click.headers["set-cookie"]
            cookie = click.cookies["lead_click_browser"]
            assert first_cookie in (None, cookie)
            first_cookie = cookie
            query = parse_qs(urlsplit(click.headers["location"]).query)
            assert query["lead_ref"] == [code]
            assert query["id"] == ["1"]
            page = client.post(
                "/collect/" + connection,
                headers={"Origin": "https://site.example"},
                json={
                    "key": token("analytics:" + connection),
                    "event_name": "PageView",
                    "event_id": uid(),
                    "anonymous_id": device,
                    "session_id": uid(),
                    "url": click.headers["location"],
                    "ref": code,
                    "click_token": query["lc"][0],
                },
            )
            assert page.status_code == 200, page.text
        process_pending_clicks(aid)
        assert day in refresh_scope(current_tenant(), aid, "live")
        response = client.get(
            chamber_analytics.REPORT_PATH, params={"day": day}, headers=auth(read_key)
        )
        reports = {
            period: client.get(
                chamber_analytics.REPORT_PATH, params={"day": day, "range": period}, headers=auth(read_key)
            ).raise_for_status()
            for period in ("today", "all")
        }
        historical = (datetime.now(TZ) - timedelta(days=1)).isoformat()
        params = [("day", day), ("range", "today"), *(("link", f"0,{code},{historical}") for code in codes)]
        records = client.get(chamber_analytics.REPORT_PATH, params=params, headers=auth(read_key)).raise_for_status()
    assert records.json() == {
        "day": day,
        "range": "today",
        "records": [{
            "slot": 0, "clicks": 2, "click_devices": 1, "pv": 2, "site_devices": 1,
            "click_devices_min": None, "site_devices_min": None,
        }],
    }
    assert response.status_code == 200, response.text
    assert response.json() == {
        "day": day,
        "status": "ready",
        "coverage": {"site": True},
        "totals": {"clicks": 2, "click_devices": 1, "pv": 2, "site_devices": 1},
        "channels": [
            {
                "channel": "github",
                "clicks": 2,
                "click_devices": 1,
                "pv": 2,
                "site_devices": 1,
            }
        ],
    }
    expected = {"clicks": 2, "click_devices": 1, "pv": 2, "site_devices": 1}
    for period, ranged in reports.items():
        assert ranged.headers["cache-control"] == "no-store"
        report = ranged.json()
        assert set(report) == {"day", "range", "pixel_connected", "history_complete", "totals", "channels", "buckets"}
        assert (report["day"], report["range"], report["pixel_connected"], report["history_complete"]) == (
            day, period, True, True,
        )
        assert report["totals"] == expected
        assert report["channels"] == [{"platform": "github", **expected}]
        assert len(report["buckets"]) == (24 if period == "today" else 30)
        assert sum(bucket["totals"]["clicks"] or 0 for bucket in report["buckets"]) == 2
        assert sum(bucket["totals"]["pv"] or 0 for bucket in report["buckets"]) == 2
        for bucket in report["buckets"]:
            assert set(bucket) == {"start", "totals", "channels"}
            assert set(bucket["totals"]) == set(expected)
            assert set(bucket["channels"][0]) == {*expected, "platform"}
        assert chamber_analytics.RangedReport.model_validate_json(ranged.content).range == period


def test_ranged_report_requires_read_key_and_current_beijing_day(production, monkeypatch):
    _, _, read_key, link_key = production
    day = datetime.now(TZ).date()
    yesterday = day - timedelta(days=1)
    path = chamber_analytics.REPORT_PATH
    with TestClient(app, base_url="https://collector.example") as client:
        params = {"day": day.isoformat(), "range": "today"}
        assert client.get(path, params=params).status_code == 401
        assert client.get(path, params=params, headers=auth(link_key)).status_code == 401
        assert client.get(path, params={**params, "range": "week"}, headers=auth(read_key)).status_code == 422
        assert client.get(path, params={**params, "day": yesterday.isoformat()}, headers=auth(read_key)).status_code == 422
        assert client.get(path, params={"day": yesterday.isoformat()}, headers=auth(read_key)).status_code == 200
        # Traffic on the same site from a separate Agent stays outside the report.
        other = domain.create_agent(runtime_engine="growth")
        with tx(other) as c:
            link = distribution.register(
                c, other, "x", "https://site.example/", intent="fixture:other-agent", placement="manual"
            )
            c.execute(
                """INSERT INTO lead.visit_sessions
                   (tenant_id,agent_id,tracking_link_id,source,source_session_id,trust_level,
                    rule_version,started_at,last_seen_at)
                   VALUES(lead.ctx_tenant(),lead.ctx_agent(),%s,'redirect',%s,'redirect_observed','fixture',now(),now())""",
                (link["id"], uid()),
            )
        for period in ("today", "all"):
            report = client.get(path, params={**params, "range": period}, headers=auth(read_key)).raise_for_status().json()
            assert report["channels"] == []
            assert report["totals"] == {"clicks": 0, "click_devices": 0, "pv": None, "site_devices": None}
        monkeypatch.setattr(chamber_analytics, "MAX_REPORT_BYTES", 16)
        assert client.get(path, params=params, headers=auth(read_key)).status_code == 503


def test_production_report_refuses_to_mix_different_sites(production):
    aid, _, read_key, _ = production
    with tx(aid) as c:
        c.execute(
            """INSERT INTO lead.analytics_connections
               (tenant_id,agent_id,provider,site_host,state,secret_ref)
               VALUES(lead.ctx_tenant(),%s,'lead_site','https://other.example','disconnected',
                      'hmac:client-write')""",
            (aid,),
        )
    day = datetime.now(TZ).date().isoformat()
    with TestClient(app, base_url="https://collector.example") as client:
        for params in ({"day": day}, {"day": day, "range": "today"}):
            response = client.get(chamber_analytics.REPORT_PATH, params=params, headers=auth(read_key))
            assert response.status_code == 503
            assert "other.example" not in response.text


def test_local_bridge_uses_only_configured_agent_and_separate_credentials(monkeypatch):
    aid, read_key, link_key = uid(), token_urlsafe(32), token_urlsafe(32)
    monkeypatch.setenv(chamber_analytics.ORIGIN, "https://collector.example/")
    monkeypatch.setenv(chamber_analytics.LOCAL_AGENT, aid)
    monkeypatch.setenv(chamber_analytics.READ_KEY, read_key)
    monkeypatch.setenv(chamber_analytics.LINK_KEY, link_key)
    day = "2026-11-20"
    report = {
        "day": day,
        "status": "ready",
        "coverage": {"site": True},
        "totals": {"clicks": 2, "click_devices": 1, "pv": 2, "site_devices": 1},
        "channels": [],
    }
    outbound = []
    blocked_report = False

    def remote(request):
        outbound.append(request)
        if blocked_report:
            return httpx.Response(
                302, headers={"Location": "https://untrusted.example/"}
            )
        if request.method == "GET":
            return httpx.Response(
                200, stream=httpx.ByteStream(json.dumps(report).encode())
            )
        return httpx.Response(204)

    monkeypatch.setattr(
        chamber_analytics.httpx,
        "Client",
        partial(httpx.Client, transport=httpx.MockTransport(remote)),
    )
    assert chamber_analytics.fetch_daily_report(uid(), day) is None
    assert not outbound
    assert chamber_analytics.fetch_daily_report(aid, day) == report
    source = {
        "code": token_urlsafe(12),
        "platform": "github",
        "destination_url": "https://site.example/",
    }
    chamber_analytics.register_production_link(
        aid, source["code"], source["platform"], source["destination_url"]
    )
    assert [str(request.url) for request in outbound] == [
        "https://collector.example" + chamber_analytics.REPORT_PATH + "?day=" + day,
        "https://collector.example" + chamber_analytics.LINKS_PATH,
    ]
    assert [request.headers["authorization"] for request in outbound] == [
        auth(read_key)["Authorization"],
        auth(link_key)["Authorization"],
    ]
    assert json.loads(outbound[1].content) == source
    blocked_report = True
    with pytest.raises(chamber_analytics.ChamberAnalyticsUnavailable) as error:
        chamber_analytics.fetch_daily_report(aid, day)
    assert len(outbound) == 3
    assert "untrusted.example" not in str(error.value)
    assert read_key not in str(error.value)
    monkeypatch.setenv(chamber_analytics.ORIGIN, "http://collector.example")
    with pytest.raises(chamber_analytics.ChamberAnalyticsUnavailable):
        chamber_analytics.fetch_daily_report(aid, day)
    assert len(outbound) == 3


def ranged_report(day, period):
    start = datetime.combine(day, time.min, TZ)
    count, step = 24, timedelta(hours=1)
    if period == "all":
        count, step = 30, timedelta(days=1)
        start -= 29 * step
    totals = {"clicks": 0, "click_devices": 0, "pv": None, "site_devices": None}
    return {
        "day": day.isoformat(),
        "range": period,
        "pixel_connected": False,
        "history_complete": True,
        "totals": totals,
        "channels": [{"platform": "x", **totals}],
        "buckets": [
            {"start": (start + index * step).isoformat(), "totals": totals, "channels": [{"platform": "x", **totals}]}
            for index in range(count)
        ],
    }


def test_ranged_bridge_validates_requested_scope_and_transport(monkeypatch):
    aid, read_key, link_key = uid(), token_urlsafe(32), token_urlsafe(32)
    day = datetime.now(TZ).date()
    monkeypatch.setenv(chamber_analytics.ORIGIN, "https://collector.example/")
    monkeypatch.setenv(chamber_analytics.LOCAL_AGENT, aid)
    monkeypatch.setenv(chamber_analytics.READ_KEY, read_key)
    monkeypatch.setenv(chamber_analytics.LINK_KEY, link_key)
    report = ranged_report(day, "today")
    outbound = []

    def remote(request):
        outbound.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(report).encode()))

    monkeypatch.setattr(
        chamber_analytics.httpx, "Client",
        partial(httpx.Client, transport=httpx.MockTransport(remote)),
    )
    assert chamber_analytics.fetch_promotion_dashboard(uid(), "today", day) is None
    assert not outbound
    assert chamber_analytics.fetch_promotion_dashboard(aid, "today", day) == report
    request, = outbound
    assert str(request.url) == "https://collector.example" + chamber_analytics.REPORT_PATH + f"?day={day}&range=today"
    assert request.headers["authorization"] == auth(read_key)["Authorization"]

    for wrong_day, wrong_period in ((day, "all"), (day - timedelta(days=1), "today")):
        report = ranged_report(wrong_day, wrong_period)
        with pytest.raises(chamber_analytics.ChamberAnalyticsUnavailable):
            chamber_analytics.fetch_promotion_dashboard(aid, "today", day)

    report = ranged_report(day, "all")
    assert chamber_analytics.fetch_promotion_dashboard(aid, "all", day) == report
    report["totals"] = {**report["totals"], "clicks": 3, "click_devices": None, "click_devices_min": 2}
    assert chamber_analytics.fetch_promotion_dashboard(aid, "all", day, device_bounds=True) == report
    assert outbound[-1].url.params["device_bounds"] == "true"
    monkeypatch.setattr(chamber_analytics, "MAX_REPORT_BYTES", 16)
    with pytest.raises(chamber_analytics.ChamberAnalyticsUnavailable):
        chamber_analytics.fetch_promotion_dashboard(aid, "all", day)


@pytest.mark.parametrize("invalid", [
    "numeric_count", "wrong_flags", "wrong_bucket_count", "wrong_bucket_order",
    "naive_start", "duplicate_channel", "duplicate_bucket_channel", "unknown_bucket_channel", "raw_identity",
])
def test_ranged_report_rejects_invalid_payload(invalid):
    report = ranged_report(datetime.now(TZ).date(), "today")
    buckets = report["buckets"]
    if invalid == "numeric_count":
        report["totals"]["clicks"] = "0"
    elif invalid == "wrong_flags":
        report["pixel_connected"] = "false"
    elif invalid == "wrong_bucket_count":
        buckets.pop()
    elif invalid == "wrong_bucket_order":
        buckets[0], buckets[1] = buckets[1], buckets[0]
    elif invalid == "naive_start":
        buckets[0]["start"] = buckets[0]["start"].split("+")[0]
    elif invalid == "duplicate_channel":
        report["channels"].append(report["channels"][0].copy())
    elif invalid == "duplicate_bucket_channel":
        buckets[0]["channels"].append(buckets[0]["channels"][0].copy())
    elif invalid == "unknown_bucket_channel":
        buckets[0]["channels"][0]["platform"] = "discord"
    elif invalid == "raw_identity":
        buckets[0]["channels"][0]["device_id"] = uid()
    with pytest.raises(chamber_analytics.ValidationError):
        chamber_analytics.RangedReport.model_validate_json(json.dumps(report))


def _registered_fixture_link(c, aid, platform="telegram", *, placement="chamber", destination="https://site.example/"):
    code = token_urlsafe(12)
    return distribution.register(
        c, aid, platform, destination, intent="chamber:" + code, placement=placement, code=code
    )


def _redirect_fact(c, link, at, device, *, exclusion=None):
    c.execute(
        """INSERT INTO lead.visit_sessions
           (tenant_id,agent_id,tracking_link_id,source,source_session_id,trust_level,rule_version,
            started_at,last_seen_at,anonymous_id,exclusion_reason)
           VALUES(lead.ctx_tenant(),lead.ctx_agent(),%s,'redirect',%s,'redirect_observed','fixture',%s,%s,%s,%s)""",
        (link["id"], uid(), at, at, device, exclusion),
    )


def _page_fact(c, connection, link, at, device, *, counted=True):
    visit = one(c, """INSERT INTO lead.visit_sessions
        (tenant_id,agent_id,tracking_link_id,source,source_session_id,trust_level,rule_version,started_at,last_seen_at)
        VALUES(lead.ctx_tenant(),lead.ctx_agent(),%s,%s,%s,'client_observed','fixture',%s,%s) RETURNING id""",
        (link["id"], "site:" + connection, uid(), at, at))["id"]
    c.execute("""INSERT INTO lead.pixel_receipts
          (tenant_id,agent_id,connection_id,event_id,event_name,anonymous_id,visit_session_id,
           trust_level,counted,payload_sha256,occurred_at,received_at)
          VALUES(lead.ctx_tenant(),lead.ctx_agent(),%s,%s,'PageView',%s,%s,'client_observed',%s,%s,%s,%s)""",
        (connection, uid(), device, visit, counted, "a"*64, at, at))


def test_device_bounds_cover_historical_snapshots_and_current_facts_only_on_opt_in(production):
    aid, connection, read_key, _ = production
    now = datetime.now(TZ)
    old = now - timedelta(days=45)
    with tx(aid) as c:
        link = _registered_fixture_link(c, aid)
        shared_device = uid()
        for when, devices in (
            (old, [shared_device, uid(), uid(), None]),
            (now, [shared_device, uid(), uid(), None, None]),
        ):
            for device in devices:
                _redirect_fact(c, link, when, device)
        diagnostic = _registered_fixture_link(c, aid, "diagnostic", placement="diagnostic")
        _redirect_fact(c, diagnostic, now, uid())
        c.execute("UPDATE lead.analytics_connections SET last_pageview_at=%s WHERE id=%s", (now, connection))
        _page_fact(c, connection, link, now, uid())
        _page_fact(c, connection, link, now, None)
        _page_fact(c, connection, link, now, uid(), counted=False)
    refresh_scope(current_tenant(), aid, "live")
    day = now.date().isoformat()
    with TestClient(app, base_url="https://collector.example") as client:
        path = chamber_analytics.REPORT_PATH
        headers = auth(read_key)
        legacy = client.get(path, params={"day": day, "range": "all"}, headers=headers).raise_for_status()
        assert legacy.json()["totals"] == {"clicks": 9, "click_devices": None, "pv": 2, "site_devices": None}
        assert b"devices_min" not in legacy.content
        for period, count, minimum in (("today", 5, 3), ("all", 9, 5)):
            report = client.get(path, params={
                "day": day, "range": period, "device_bounds": "true",
            }, headers=headers).raise_for_status().json()
            assert report["totals"] == {
                "clicks": count, "click_devices": None, "click_devices_min": minimum,
                "pv": 2, "site_devices": None, "site_devices_min": 1,
            }
            assert report["channels"] == [{"platform": "telegram", **report["totals"]}]
            assert any(bucket["totals"].get("click_devices_min") == 3 for bucket in report["buckets"])
        publication = (now - timedelta(days=50)).isoformat()
        entry = f"1,{link['public_code']},{publication}"
        records = client.get(path, params={"day": day, "range": "all", "link": entry}, headers=headers)
        assert records.raise_for_status().json()["records"] == [{
            "slot": 1, "clicks": 9, "click_devices": None, "click_devices_min": 5,
            "pv": 2, "site_devices": None, "site_devices_min": 1,
        }]


def test_link_report_restricts_window_registration_and_scope(production):
    aid, _, read_key, link_key = production
    now = datetime.now(TZ)
    published_at = datetime.combine(now.date(), time.min, TZ)
    with tx(aid) as c:
        registered = _registered_fixture_link(c, aid)
        ordinary = _registered_fixture_link(c, aid, placement="manual")
        wrong_origin = _registered_fixture_link(c, aid, destination="https://site.example:444/")
        for link in (registered, ordinary, wrong_origin):
            _redirect_fact(c, link, now, uid())
        _redirect_fact(c, registered, published_at - timedelta(seconds=1), uid())
        _redirect_fact(c, registered, now, uid(), exclusion="known_bot")
        _redirect_fact(c, registered, now, uid(), exclusion="coverage_probe")
        _redirect_fact(c, registered, now + timedelta(days=1), uid())
    other = domain.create_agent(runtime_engine="growth")
    with tx(other) as c:
        foreign = _registered_fixture_link(c, other)
        _redirect_fact(c, foreign, now, uid())

    def link_param(slot, link, start=published_at):
        return ("link", f"{slot},{link['public_code']},{start.isoformat()}")

    day = now.date().isoformat()
    path = chamber_analytics.REPORT_PATH
    params = [("day", day), ("range", "today")]
    valid = link_param(0, registered)
    mixed = link_param(0, ordinary)
    with TestClient(app, base_url="https://collector.example") as client:
        assert client.get(path, params=[*params, valid], headers=auth(link_key)).status_code == 401
        assert client.get(path, params=[("day", day), valid], headers=auth(read_key)).status_code == 422
        for links in (
            [valid, valid],
            [valid, link_param(0, ordinary, published_at - timedelta(days=1))],
            [link_param(10, registered)],
            [link_param(0, registered, published_at.replace(tzinfo=None))],
            [link_param(0, registered, now + timedelta(days=1))],
            [valid, ("device_bounds", "true")],
            [valid] * 41,
        ):
            assert client.get(path, params=[*params, *links], headers=auth(read_key)).status_code == 422
        valid_and_unmapped = [
            *params, valid, link_param(1, ordinary), link_param(2, wrong_origin), link_param(3, foreign)
        ]
        report = client.get(path, params=valid_and_unmapped, headers=auth(read_key)).raise_for_status().json()
        assert report["records"] == [{
            "slot": 0, "clicks": 1, "click_devices": 1, "click_devices_min": None,
            "pv": None, "site_devices": None, "site_devices_min": None,
        }]
        report = client.get(path, params=[*params, valid, mixed], headers=auth(read_key)).raise_for_status().json()
        assert report["records"] == []


def test_link_bridge_only_requests_and_accepts_bounded_slots(monkeypatch):
    aid, read_key, link_key = uid(), token_urlsafe(32), token_urlsafe(32)
    now = datetime.now(TZ)
    day = now.date()
    code = token_urlsafe(12)
    link = chamber_analytics.RecordLink(slot=2, code=code, published_at=now)
    monkeypatch.setenv(chamber_analytics.ORIGIN, "https://collector.example")
    monkeypatch.setenv(chamber_analytics.LOCAL_AGENT, aid)
    monkeypatch.setenv(chamber_analytics.READ_KEY, read_key)
    monkeypatch.setenv(chamber_analytics.LINK_KEY, link_key)
    requests = []
    totals = {
        "clicks": 6, "click_devices": None, "click_devices_min": 4,
        "pv": None, "site_devices": None, "site_devices_min": None,
    }
    response = {"day": day.isoformat(), "range": "all", "records": [{"slot": 2, **totals}]}

    def remote(request):
        requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(response).encode()))

    monkeypatch.setattr(chamber_analytics.httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(remote)))
    assert chamber_analytics.fetch_promotion_links(uid(), "all", day, [link]) is None
    assert chamber_analytics.fetch_promotion_links(aid, "all", day, []) == {}
    assert not requests
    assert chamber_analytics.fetch_promotion_links(aid, "all", day, [link]) == {2: totals}
    request, = requests
    assert request.url.path == chamber_analytics.REPORT_PATH
    assert request.url.params.get_list("link") == [f"2,{code},{now.isoformat()}"]
    assert request.headers["authorization"] == auth(read_key)["Authorization"]
    for invalid in (
        [{"slot": 1, **totals}],
        [{"slot": 2, **totals, "device_id": uid()}],
        [{"slot": 2, **totals, "click_devices_min": 7}],
        [{"slot": 2, **totals}, {"slot": 2, **totals}],
    ):
        response["records"] = invalid
        with pytest.raises(chamber_analytics.ChamberAnalyticsUnavailable):
            chamber_analytics.fetch_promotion_links(aid, "all", day, [link])
