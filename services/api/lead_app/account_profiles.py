"""Resolve a login slot inside the project's tenant scope, never by display name."""

from uuid import UUID

from .atomic_dispatch import channel_pilot
from .config import STATE
from .db import one, require_agent, tx, uid
from .operating_playbooks import channel_for


def directory(aid, profile, node_root=STATE):
    root = node_root / "profiles" / str(UUID(str(aid)))
    return (
        root if profile["slot"] == "default" else root / "accounts" / str(profile["id"])
    )


def for_platform(aid, platform, *, create=False, account_id=None, new_binding=False):
    with tx(aid) as c:
        agent = require_agent(c, aid, create)
        if new_binding and (
            account_id
            or not create
            or not channel_pilot(aid)
            or not channel_for(platform)
        ):
            raise ValueError("此渠道不能创建额外账号环境")
        if not new_binding:
            existing = one(
                c,
                """SELECT p.*,s.id AS account_id FROM lead.social_accounts s JOIN lead.browser_profiles p
                ON p.id=s.profile_id WHERE s.platform=%s AND (%s::uuid IS NULL OR s.id=%s)
                ORDER BY s.created_at DESC LIMIT 1""",
                (platform, account_id, account_id),
            )
            if existing:
                return existing
            if account_id:
                raise ValueError("此账号不属于当前项目与渠道")
        base = one(c, "SELECT * FROM lead.browser_profiles WHERE slot='default'")
        if not create or not channel_pilot(aid) or not channel_for(platform):
            return base
        slot = platform if not new_binding else platform + "-" + uid()
        existing = one(c, "SELECT * FROM lead.browser_profiles WHERE slot=%s", (slot,))
        if existing:
            return existing
        pid = uid()
        root = directory(aid, {"slot": platform, "id": pid})
        profile = one(
            c,
            """INSERT INTO lead.browser_profiles
            (id,tenant_id,agent_id,provider,engine_version,sdk_version,config_ref,config_hash,
             storage_ref,key_material_ref,network_identity_id,slot)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                pid,
                agent["tenant_id"],
                aid,
                base["provider"],
                base["engine_version"],
                base["sdk_version"],
                str(root / "identity.json"),
                pid,
                str(root / "storage"),
                base["key_material_ref"],
                base["network_identity_id"],
                slot,
            ),
        )
        c.execute(
            """INSERT INTO lead.profile_leases(profile_id,tenant_id,agent_id,control_version)
            VALUES(%s,%s,%s,%s)""",
            (pid, agent["tenant_id"], aid, agent["control_version"]),
        )
        if new_binding:
            profile["account_id"] = one(
                c,
                """INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,automation_enabled)
                VALUES(%s,%s,%s,%s,'pending',false) RETURNING id""",
                (agent["tenant_id"], aid, pid, platform),
            )["id"]
        return profile


def takeover_profile(aid, takeover):
    with tx(aid) as c:
        row = one(
            c,
            """SELECT profile_id FROM lead.takeover_sessions WHERE id=%s AND state='active'
            AND expires_at>now()""",
            (takeover,),
        )
        if not row:
            raise ValueError("接管已过期")
        return row["profile_id"]
