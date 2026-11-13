"""A paired device retains its creator's current workspace access, not a snapshot."""
from .admin_permissions import resolve
from .config import env
from .db import one
from .scope import current_tenant, principal


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
