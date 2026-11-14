"""A paired device retains its creator's current workspace access, not a snapshot."""
from .admin_permissions import resolve
from .config import env
from .db import one
from .scope import current_tenant, principal, trusted_execution


def _local_owner_mode():
    values = env()
    return (values.get("LEAD_DEPLOYMENT_MODE") != "production"
            and values.get("LEAD_AUTH_MODE", "local_owner") == "local_owner")


def capture(connection, aid):
    """Only the verified HTTP principal, or actual local-owner mode, may pair."""
    who = principal()
    if who and who.get("issuer") != "local":
        if who.get("tenant_id") != current_tenant():
            raise ValueError("executor_device_unavailable")
        owner = {"issuer": who.get("issuer"), "subject": who.get("subject")}
        require_current(connection, owner, aid)
        return owner
    if _local_owner_mode() and (not who or who.get("role") == "owner"):
        return {"local_owner": True}
    raise ValueError("executor_device_unavailable")


def require_current(connection, owner, aid):
    """Use the existing membership and published capability rules in one tx."""
    if not isinstance(owner, dict):
        raise ValueError("executor_device_unavailable")
    if owner.get("local_owner") is True:
        if _local_owner_mode():
            return
        raise ValueError("executor_device_unavailable")
    issuer, subject = owner.get("issuer"), owner.get("subject")
    if not isinstance(issuer, str) or not issuer or not isinstance(subject, str) or not subject:
        raise ValueError("executor_device_unavailable")
    member = one(connection, """SELECT m.role,m.status FROM lead.memberships m
        JOIN lead.tenants t ON t.id=m.tenant_id AND t.deleted_at IS NULL
        WHERE m.tenant_id=lead.ctx_tenant() AND m.oidc_issuer=%s AND m.oidc_subject=%s""", (issuer, subject))
    if not member or member["status"] != "active" or member["role"] not in {"owner", "operator"}:
        raise ValueError("executor_device_unavailable")
    identity = {"tenant_id": current_tenant(), "issuer": issuer, "subject": subject, "role": member["role"]}
    access = resolve(identity, connection=connection)
    allowed = access.get("agent_ids")
    if ("agents.control" not in access["capabilities"]
            or allowed is not None and str(aid) not in {str(value) for value in allowed}):
        raise ValueError("executor_device_unavailable")


def capture_studio(connection, purpose):
    """Studio work needs a real HTTP actor; a service boolean is not a job."""
    who = principal()
    if not who or who.get('role') not in {'owner', 'operator'} or who.get('tenant_id') != current_tenant():
        raise ValueError('executor_studio_actor_unavailable')
    if purpose != 'website_brief':
        raise ValueError('executor_studio_purpose_unavailable')
    actor = {'actor_type': 'user', 'tenant_id': current_tenant(), 'issuer': who.get('issuer'),
             'subject': who.get('subject'), 'purpose': purpose}
    require_studio_actor(connection, actor)
    import hashlib
    import json
    actor['actor_ref'] = 'user:' + hashlib.sha256(json.dumps(actor, sort_keys=True).encode()).hexdigest()
    return actor


def require_studio_actor(connection, actor):
    if (not isinstance(actor, dict) or actor.get('actor_type') != 'user'
            or actor.get('tenant_id') != current_tenant() or actor.get('purpose') != 'website_brief'
            or not isinstance(actor.get('issuer'), str) or not actor['issuer']
            or not isinstance(actor.get('subject'), str) or not actor['subject']):
        raise ValueError('executor_studio_actor_unavailable')
    if actor['issuer'] == 'local':
        if not _local_owner_mode() or actor['subject'] != 'local':
            raise ValueError('executor_studio_actor_unavailable')
        return
    member = one(connection, """SELECT m.role,m.status FROM lead.memberships m
        JOIN lead.tenants t ON t.id=m.tenant_id AND t.deleted_at IS NULL
        WHERE m.tenant_id=lead.ctx_tenant() AND m.oidc_issuer=%s AND m.oidc_subject=%s""",
        (actor['issuer'], actor['subject']))
    if not member or member['status'] != 'active' or member['role'] not in {'owner', 'operator'}:
        raise ValueError('executor_studio_actor_unavailable')
    access = resolve({**actor, 'role': member['role']}, connection=connection)
    if 'agents.create' not in access['capabilities'] or access.get('agent_ids') is not None:
        raise ValueError('executor_studio_actor_unavailable')


def service_device(connection, node, grant, epoch):
    """A real Node/grant may enter typed execution, never v1 creator auth."""
    who = principal()
    if (not trusted_execution() or not who or who.get('actor_type') != 'service'
            or who.get('role') != 'service' or who.get('node_id') != str(node['id'])
            or grant['node_id'] != node['id'] or str(grant['tenant_id']) != current_tenant()
            or grant['scope_kind'] != 'project' or grant['state'] != 'active'
            or who.get('agent_ids') != [str(grant['agent_id'])]):
        raise ValueError('executor_scope_forbidden')
    device = one(connection, 'SELECT * FROM lead.local_executor_devices WHERE id=%s FOR UPDATE', (grant['device_id'],))
    if (not device or device['state'] != 'active' or device['token_hash'] is not None
            or device['tenant_id'] != grant['tenant_id'] or device['agent_id'] != grant['agent_id']):
        raise ValueError('executor_scope_forbidden')
    if device['epoch'] != epoch:
        raise ValueError('executor_epoch_changed')
    return device
