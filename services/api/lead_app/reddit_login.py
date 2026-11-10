"""A separately assigned Reddit exit only affects isolated account profiles."""

from ipaddress import IPv4Address
from uuid import NAMESPACE_URL, uuid5

from .config import env
from .db import one, require_agent, tx

PROVIDER = "reddit-login-ipv4"
SOCKET = "socks5+unix:///run/lead-egress/reddit-v4.sock"


def split_reddit_login(aid):
    projects = env().get("LEAD_SPLIT_REDDIT_LOGIN_AGENT_IDS") or ""
    return str(aid) in {entry.strip() for entry in projects.split(",")}


def assign_exit(aid, profile):
    slot = profile["slot"]
    if slot != "reddit" and not slot.startswith("reddit-"):
        return None
    network_id = uuid5(NAMESPACE_URL, f"lead:{aid}:{PROVIDER}")
    expected = env().get("LEAD_REDDIT_IPV4_" + str(aid).replace("-", "").upper())
    if not expected:
        if str(profile["network_identity_id"]) == str(network_id):
            raise ValueError("此 Reddit 账号的固定出口尚未配置")
        return None
    address = IPv4Address(expected)
    if not address.is_global or address.is_multicast:
        raise ValueError("Reddit 登录需要固定公网 IPv4")
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
            raise ValueError("Reddit 出口绑定已变化，需要重新核对")
        c.execute(
            """UPDATE lead.browser_profiles SET network_identity_id=%s
            WHERE id=%s AND network_identity_id IS DISTINCT FROM %s""",
            (network_id, profile["id"], network_id),
        )
    profile["network_identity_id"] = network_id
    return {"server": SOCKET}
