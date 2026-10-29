import pytest
from lead_app import temporal_client


def test_production_cannot_connect_to_plaintext_task_service(monkeypatch):
    monkeypatch.setattr(
        temporal_client, "env", lambda: {"LEAD_DEPLOYMENT_MODE": "production"}
    )
    with pytest.raises(ValueError, match="TLS"):
        temporal_client.connection_options()
    monkeypatch.setattr(
        temporal_client, "env", lambda: {"LEAD_TEMPORAL_API_KEY": "synthetic"}
    )
    with pytest.raises(ValueError, match="明文"):
        temporal_client.connection_options()


def test_tls_requires_paired_credentials_and_reads_certificates(tmp_path, monkeypatch):
    cert = tmp_path / "cert"
    key = tmp_path / "key"
    ca = tmp_path / "ca"
    cert.write_bytes(b"synthetic-certificate")
    key.write_bytes(b"synthetic-private-key")
    ca.write_bytes(b"synthetic-ca")
    values = {
        "LEAD_DEPLOYMENT_MODE": "production",
        "LEAD_TEMPORAL_ADDRESS": "workflow.example:7233",
        "LEAD_TEMPORAL_NAMESPACE": "lead-prod",
        "LEAD_TEMPORAL_TLS": "1",
        "LEAD_TEMPORAL_CERT_FILE": str(cert),
    }
    monkeypatch.setattr(temporal_client, "env", lambda: values)
    with pytest.raises(ValueError, match="成对"):
        temporal_client.connection_options()
    values.update(LEAD_TEMPORAL_KEY_FILE=str(key), LEAD_TEMPORAL_CA_FILE=str(ca))
    result = temporal_client.connection_options()
    assert (
        result["namespace"] == "lead-prod"
        and result["target_host"] == "workflow.example:7233"
    )
    assert result["tls"].client_cert == cert.read_bytes()
    assert result["tls"].client_private_key == key.read_bytes()
    assert result["tls"].server_root_ca_cert == ca.read_bytes()
