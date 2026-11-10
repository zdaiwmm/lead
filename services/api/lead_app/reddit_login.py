"""Bind eligible isolated login profiles to the project's existing Tokyo IPv4."""

from ipaddress import IPv4Address
from uuid import NAMESPACE_URL, uuid5

from .account_profiles import isolated_login_platforms
from .config import env
from .db import one, require_agent, tx

PROVIDER = "reddit-login-ipv4"
SOCKET = "socks5+unix:///run/lead-egress/reddit-v4.sock"


def assign_exit(aid, profile):
    platform = profile["slot"].split("-", 1)[0]
    network_id = uuid5(NAMESPACE_URL, f"lead:{aid}:{PROVIDER}")
    if platform != "reddit":
        if platform not in ("facebook", "x"):
            return None
        if platform not in isolated_login_platforms(aid):
            if str(profile["network_identity_id"]) == str(network_id):
                raise ValueError("此账号的东京登录出口已停用，请核对配置")
            return None
    expected = env().get("LEAD_REDDIT_IPV4_" + str(aid).replace("-", "").upper())
    if not expected:
        if platform != "reddit" or str(profile["network_identity_id"]) == str(network_id):
            raise ValueError("此账号的固定出口尚未配置")
        return None
    address = IPv4Address(expected)
    if not address.is_global or address.is_multicast:
        raise ValueError("账号登录需要固定公网 IPv4")
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        c.execute(
            """INSERT INTO lead.network_identities
            (id,tenant_id,agent_id,provider_ref,endpoint_ref,secret_ref,mode,expected_ip,expected_region,policy_version)
            VALUES(%s,%s,%s,%s,%s,'local-unix-socket','dedicated',%s,'JP',1)
            ON CONFLICT (id) DO NOTHING""",
            (network_id, agent["tenant_id"], aid, PROVIDER, SOCKET, str(address)),
        )
        route = one(
            c,
            """SELECT id FROM lead.network_identities WHERE id=%s
            AND provider_ref=%s AND endpoint_ref=%s AND expected_ip=%s AND state<>'retired'""",
            (network_id, PROVIDER, SOCKET, str(address)),
        )
        if not route:
            raise ValueError("账号出口绑定已变化，需要重新核对")
        c.execute(
            """UPDATE lead.browser_profiles SET network_identity_id=%s
            WHERE id=%s AND network_identity_id IS DISTINCT FROM %s""",
            (network_id, profile["id"], network_id),
        )
    profile["network_identity_id"] = network_id
    return {"server": SOCKET}
