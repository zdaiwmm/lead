"""Device grants follow current workspace membership and project permissions."""
from uuid import uuid4

import psycopg
import pytest
from lead_app import admin_config
from lead_app import local_executor as executor
from lead_app.config import database_kwargs
from lead_app.db import tx
from lead_app.scope import current_tenant, tenant_scope


def member(role="operator"):
    who = {"tenant_id": current_tenant(), "issuer": "https://identity.example/"+uuid4().hex,
           "subject": uuid4().hex, "role": role, "capabilities": ["agents.control"], "agent_ids": None}
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute("""INSERT INTO lead.memberships(tenant_id,oidc_issuer,oidc_subject,role,status)
            VALUES(%s,%s,%s,%s,'active')""", (who["tenant_id"], who["issuer"], who["subject"], role))
    return who


def policy(who, aids=None, capabilities=None):
    with tx() as c:
        old = admin_config.read(c, "member-access/default")
        # Each case owns this fixture policy; earlier disposable projects have
        # already been archived and must not enter a new policy's validation.
        entries = []
        entries.append({"issuer": who["issuer"], "subject": who["subject"], "agent_ids": aids,
                        "capabilities": ["agents.control"] if capabilities is None else capabilities})
        admin_config.write(c, "member-access/default", {"members": entries}, old["version"])


def connect(aid, who):
    with tenant_scope(who["tenant_id"], who):
        invitation = executor.create_pairing(aid)
    return executor.pair(invitation["code"], "Member device", "darwin", {"ready": True})


@pytest.mark.parametrize("change", ["revoked", "removed", "viewer"])
def test_removed_member_cannot_keep_polling_existing_device(growth_agent, change):
    who = member()
    device = connect(growth_agent, who)
    assert executor.poll(device["token"], 1)["epoch"] == 1
    with psycopg.connect(**database_kwargs(True)) as c:
        if change == "removed":
            c.execute("DELETE FROM lead.memberships WHERE tenant_id=%s AND oidc_issuer=%s AND oidc_subject=%s",
                      (who["tenant_id"], who["issuer"], who["subject"]))
        else:
            column = "status" if change == "revoked" else "role"
            from psycopg import sql
            c.execute(sql.SQL("UPDATE lead.memberships SET {}=%s WHERE tenant_id=%s AND oidc_issuer=%s AND oidc_subject=%s").format(sql.Identifier(column)),
                      (change, who["tenant_id"], who["issuer"], who["subject"]))
    with pytest.raises(ValueError, match="executor_device_unavailable"):
        executor.poll(device["token"], 1)


def test_agent_scope_and_control_revocation_take_effect_without_repairing(growth_agent):
    who = member()
    policy(who, [str(growth_agent)])
    device = connect(growth_agent, who)
    executor.poll(device["token"], 1)
    policy(who, [])
    with pytest.raises(ValueError, match="executor_device_unavailable"):
        executor.poll(device["token"], 1)
    policy(who, [str(growth_agent)])
    executor.poll(device["token"], 1)
    policy(who, [str(growth_agent)], [])
    with pytest.raises(ValueError, match="executor_device_unavailable"):
        executor.poll(device["token"], 1)


def test_old_pairing_invitation_does_not_restore_revoked_membership(growth_agent):
    who = member("owner")
    with tenant_scope(who["tenant_id"], who):
        invitation = executor.create_pairing(growth_agent)
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute("UPDATE lead.memberships SET status='revoked' WHERE tenant_id=%s AND oidc_issuer=%s AND oidc_subject=%s",
                  (who["tenant_id"], who["issuer"], who["subject"]))
    with pytest.raises(ValueError, match="executor_device_unavailable"):
        executor.pair(invitation["code"], "Old invitation", "darwin", {"ready": True})


def test_production_oidc_requires_actual_pairing_principal(growth_agent, monkeypatch):
    monkeypatch.setenv("LEAD_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("LEAD_AUTH_MODE", "oidc")
    with tenant_scope(current_tenant(), None), pytest.raises(ValueError, match="executor_device_unavailable"):
        executor.create_pairing(growth_agent)


def test_local_owner_compatibility_does_not_survive_switch_to_oidc(growth_agent, monkeypatch):
    monkeypatch.setenv("LEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("LEAD_AUTH_MODE", "local_owner")
    invitation = executor.create_pairing(growth_agent)
    device = executor.pair(invitation["code"], "Local fixture", "darwin", {"ready": True})
    assert executor.poll(device["token"], 1)["epoch"] == 1
    monkeypatch.setenv("LEAD_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("LEAD_AUTH_MODE", "oidc")
    with pytest.raises(ValueError, match="executor_device_unavailable"):
        executor.poll(device["token"], 1)


def test_device_control_capability_does_not_require_editing_or_allow_chat(growth_agent):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lead_app.admin_permissions import workbench_permission
    from lead_app.local_executor_api import router
    who = member()
    policy(who, [str(growth_agent)], ["agents.control"])
    device = connect(growth_agent, who)
    request = executor.submit(growth_agent, "control-only", {"input": "Read evidence", "instructions": "Observe", "tools": []},
                              session_key="control-only")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    base = f"/api/agents/{growth_agent}/executor"
    routes = [("POST", base+"/pairing"), ("POST", base+"/mode"),
              ("POST", base+f"/devices/{device['device_id']}/revoke"),
              ("DELETE", base+f"/devices/{device['device_id']}"),
              ("POST", base+f"/requests/{request['id']}/commands")]
    for method, path in routes:
        assert workbench_permission(method, path) is None
    assert workbench_permission("POST", base+"/arbitrary") == "agents.edit"
    with tenant_scope(who["tenant_id"], who):
        assert client.post(base+"/pairing").status_code == 200
        assert client.post(base+f"/requests/{request['id']}/commands", json={"kind": "steer", "payload": {"input": "change"}}).status_code == 403
        assert client.post(base+f"/requests/{request['id']}/commands", json={"kind": "interrupt", "payload": {}}).status_code == 200
        assert client.post(base+f"/devices/{device['device_id']}/revoke").status_code == 200
    viewer = {**who, "role": "viewer"}
    with tenant_scope(who["tenant_id"], viewer):
        for method, path in routes:
            response = client.request(method, path, json={"mode": "legacy"} if path.endswith("/mode") else
                                      {"kind": "interrupt", "payload": {}} if path.endswith("/commands") else None)
            assert response.status_code == 403
