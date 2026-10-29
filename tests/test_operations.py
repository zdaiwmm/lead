import io
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import boto3
import psycopg
import pytest
from botocore.stub import ANY, Stubber
from fastapi.testclient import TestClient
from lead_app import backup_store, domain, ops, telemetry
from lead_app.api import app
from lead_app.backup_crypto import encrypt_stream
from lead_app.backups import prune_managed
from lead_app.config import STATE, database_kwargs
from lead_app.db import emit, one, tx, uid
from lead_app.security import token
from psycopg.types.json import Jsonb


def test_private_monitor_metrics_and_trace_redaction(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    client = TestClient(app)
    assert client.get("/internal/metrics").status_code == 403
    client.cookies.set("lead_session", token("owner"))
    marker = "PRIVATE_PROMPT_MUST_NOT_ENTER_TRACE_" + uid()
    parent = "00-12345678901234567890123456789012-1234567890123456-01"
    result = client.post(
        "/api/agents",
        headers={
            "Origin": "http://127.0.0.1:3100",
            "X-Lead-Request": "1",
            "traceparent": parent,
        },
        json={},
    )
    assert result.status_code == 201
    aid = result.json()["id"]
    try:
        client.post(
            "/api/agents/" + aid + "/messages?private=" + marker,
            headers={"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"},
            json={"text": marker, "client_message_id": uid()},
        )
        with telemetry.span("diagnostic", attributes={"prompt": marker}):
            with tx(aid) as c:
                emit(c, aid, "diagnostic.fixture", {"not_exported": marker})
                event = one(
                    c,
                    "SELECT trace_parent,trace_id FROM lead.outbox_events WHERE event_type='diagnostic.fixture'",
                )
                assert telemetry.TRACEPARENT.fullmatch(event["trace_parent"])
                assert event["trace_id"] == telemetry.trace_id()
        assert telemetry.flush()
        rows = []
        for path in (STATE / "telemetry").glob("*.jsonl"):
            data = path.read_text()
            assert marker not in data
            rows.extend(json.loads(line) for line in data.splitlines())
        assert any(
            r["trace_id"] == "12345678901234567890123456789012"
            and r["name"] == "POST /api/agents"
            for r in rows
        )
        metrics = client.get(
            "/internal/metrics", headers={"Authorization": "Bearer " + ops.credential()}
        )
        assert metrics.status_code == 200 and "lead_http_requests_total" in metrics.text
        assert marker not in metrics.text and aid not in metrics.text
    finally:
        domain.set_control(aid, "archive")


def test_alerts_deduplicate_and_resolve_without_messages():
    key = "fixture." + uid()
    alert = {key: {"code": key, "severity": "warning"}}
    # Keep other current alerts when testing this new, isolated diagnostic key.
    with psycopg.connect(**database_kwargs(True)) as c:
        prior = {
            r[0]: {"code": r[1], "severity": r[2]}
            for r in c.execute(
                "SELECT event_key,code,severity FROM lead.ops_alerts WHERE state='active'"
            ).fetchall()
        }
    try:
        ops.persist_alerts({**prior, **alert})
        ops.persist_alerts({**prior, **alert})
        with psycopg.connect(**database_kwargs(True)) as c:
            assert c.execute(
                "SELECT occurrences,state FROM lead.ops_alerts WHERE event_key=%s",
                (key,),
            ).fetchone() == (2, "active")
        ops.persist_alerts(prior)
        with psycopg.connect(**database_kwargs(True)) as c:
            assert (
                c.execute(
                    "SELECT state FROM lead.ops_alerts WHERE event_key=%s", (key,)
                ).fetchone()[0]
                == "resolved"
            )
    finally:
        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute("DELETE FROM lead.ops_alerts WHERE event_key=%s", (key,))


def test_retention_never_removes_manual_or_foreign_directory_backups(tmp_path):
    ids = []
    paths = []
    try:
        with psycopg.connect(**database_kwargs(True)) as c:
            for index in range(6):
                identifier = str(uuid4())
                ids.append(identifier)
                folder = tmp_path if index < 5 else tmp_path / "manual"
                folder.mkdir(exist_ok=True)
                path = folder / (identifier + ".leadbackup")
                path.write_bytes(b"owned-test-backup")
                paths.append(path)
                when = datetime.now(timezone.utc) - timedelta(minutes=index)
                c.execute(
                    "INSERT INTO lead.backup_records(id,kind,started_at,completed_at,cipher_sha256,key_id,size_bytes,object_ref,manifest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        identifier,
                        "database",
                        when,
                        when,
                        "0" * 64,
                        "test",
                        path.stat().st_size,
                        str(path),
                        Jsonb({"managed": index < 5}),
                    ),
                )
        expired = prune_managed(tmp_path, maximum_bytes=0)
        assert set(expired) == set(ids[3:5])
        assert all(path.exists() for path in paths[:3]) and paths[5].exists()
    finally:
        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute(
                "DELETE FROM lead.backup_records WHERE id=ANY(%s::uuid[])", (ids,)
            )


def test_s3_upload_uses_cipher_checksum_and_conditional_create(tmp_path, monkeypatch):
    identifier = str(uuid4())
    path = tmp_path / (identifier + ".leadbackup")
    encrypt_stream(
        io.BytesIO(b"private backup bytes"),
        path,
        os.urandom(32),
        {"id": identifier, "kind": "database"},
    )
    digest = backup_store.digest(path)
    import base64

    checksum = base64.b64encode(bytes.fromhex(digest)).decode()
    s3 = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
    )
    stub = Stubber(s3)
    common = {
        "Bucket": "lead-backup-fixture",
        "Key": "encrypted/" + path.name,
        "ChecksumMode": "ENABLED",
    }
    stub.add_client_error(
        "head_object",
        service_error_code="404",
        http_status_code=404,
        expected_params=common,
    )
    stub.add_response(
        "put_object",
        {"ChecksumSHA256": checksum},
        {
            "Bucket": common["Bucket"],
            "Key": common["Key"],
            "Body": ANY,
            "ContentLength": path.stat().st_size,
            "ChecksumSHA256": checksum,
            "IfNoneMatch": "*",
            "Metadata": {"cipher-sha256": digest},
            "ChecksumAlgorithm": "SHA256",
            "ServerSideEncryption": "AES256",
        },
    )
    stub.add_response(
        "head_object",
        {
            "ContentLength": path.stat().st_size,
            "Metadata": {"cipher-sha256": digest},
            "ChecksumSHA256": checksum,
            "VersionId": "fixture-version",
        },
        common,
    )
    monkeypatch.setattr(backup_store, "client", lambda: s3)
    with stub:
        result = backup_store.upload(path, "s3://lead-backup-fixture/encrypted")
        assert result["verified"] == "server_sha256"
    stub.assert_no_pending_responses()
    plain = tmp_path / (str(uuid4()) + ".leadbackup")
    plain.write_bytes(b"plaintext")
    with pytest.raises(ValueError):
        backup_store.upload(plain, "s3://lead-backup-fixture/encrypted")
