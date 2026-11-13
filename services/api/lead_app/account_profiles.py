"""Resolve a login slot inside the project's tenant scope, never by display name."""

from uuid import UUID

from .atomic_dispatch import channel_pilot
from .config import ROOT, STATE, env
from .db import one, require_agent, tx, uid
from .operating_playbooks import channel_for


def native_available():
    return (ROOT/'services/browser-runtime/manifest.json').is_file()


def connection_origin(site_url):
    from urllib.parse import urlsplit

    from .browser_execution import origin
    scheme,host,port=origin(site_url)
    parsed=urlsplit(site_url)
    default=443 if scheme=='https' else 80
    return f"{scheme}://{parsed.netloc.lower()}" if port!=default else f"{scheme}://{host}"

def isolated_login_platforms(aid):
    settings = env()
    platforms = set()
    for key, channels in (
        ("LEAD_SPLIT_REDDIT_LOGIN_AGENT_IDS", ("reddit",)),
        ("LEAD_SPLIT_FACEBOOK_X_LOGIN_AGENT_IDS", ("facebook", "x")),
    ):
        if str(aid) in (entry.strip() for entry in settings.get(key, "").split(",")):
            platforms.update(channels)
    return platforms


def directory(aid, profile, node_root=STATE):
    root = node_root / "profiles" / str(UUID(str(aid)))
    return (
        root if profile["slot"] == "default" else root / "accounts" / str(profile["id"])
    )


def for_platform(
    aid,
    platform,
    *,
    create=False,
    account_id=None,
    new_binding=False,
    isolated_login=False,
    site_url=None,
):
    site_origin = connection_origin(site_url) if site_url else None
    if platform == 'web' and create and not site_origin and not account_id:
        raise ValueError('请输入要连接的网站网址')
    with tx(aid) as c:
        agent = require_agent(c, aid, create)
        from .adapters.catalog import SPECS
        from .growth.store import enabled
        growth = enabled(agent)
        if new_binding and (
            account_id
            or not create
            or not (growth or channel_pilot(aid))
            or not ((platform in SPECS or platform == "web") if growth else channel_for(platform))
        ):
            raise ValueError("此渠道不能创建额外账号环境")
        if not new_binding:
            existing = one(
                c,
                """SELECT p.*,s.id AS account_id FROM lead.social_accounts s JOIN lead.browser_profiles p
                ON p.id=s.profile_id WHERE s.platform=%s AND (%s::uuid IS NULL OR s.id=%s)
                AND (%s::text IS NULL OR s.connection_origin=%s)
                ORDER BY s.created_at DESC LIMIT 1""",
                (platform, account_id, account_id, site_origin if platform == "web" else None, site_origin),
            )
            if existing:
                if isolated_login and existing["slot"] == "default":
                    raise ValueError("此账号仍使用原浏览器环境，请先核对绑定资料")
                return existing
            if account_id:
                raise ValueError("此账号不属于当前项目与渠道")
        base = one(c, "SELECT * FROM lead.browser_profiles WHERE slot='default'")
        if not create or not (
            growth or isolated_login or (channel_pilot(aid) and channel_for(platform))
        ):
            return base
        native = growth and native_available() and platform != 'lab'
        if native and not site_origin:
            from .domain import PLATFORMS
            site_origin=connection_origin(next(url for name,_,url in PLATFORMS if name==platform))
        slot = platform if not new_binding else platform + "-" + uid()
        if native:
            # Different sites may share the complete instance. Another identity
            # on the same origin gets a separate persistent profile.
            from urllib.parse import urlsplit

            from .execution_routes import default_transport
            hostname=urlsplit(site_origin).hostname
            # A shared cookie Domain must be a common suffix. Comparing the last
            # two labels is deliberately conservative for multi-label public
            # suffixes: it may split extra instances, never share sibling sites.
            identity_site='.'.join(hostname.split('.')[-2:])
            members=c.execute("""SELECT s.connection_origin,s.platform,r.transport_ref FROM lead.social_accounts s
                JOIN lead.browser_profiles p ON p.id=s.profile_id
                LEFT JOIN lead.profile_execution_routes r ON r.profile_id=p.id WHERE p.slot='native-shared'""").fetchall()
            conflict=any('.'.join(urlsplit(member['connection_origin']).hostname.split('.')[-2:])==identity_site
                for member in members if member.get('connection_origin'))
            tokyo={'reddit'} | (isolated_login_platforms(aid) & {'facebook','x'})
            conflict=conflict or any((member['transport_ref'] or default_transport(member['platform']))!=default_transport(platform)
                or (member['platform'] in tokyo)!=(platform in tokyo) for member in members)
            slot='native-'+uid() if new_binding or conflict else 'native-shared'
        profile = one(c, "SELECT * FROM lead.browser_profiles WHERE slot=%s", (slot,))
        if not profile:
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
                    "chromium" if native else base["provider"],
                    "pending" if native else base["engine_version"],
                    "browser-use/0.13.14" if native else base["sdk_version"],
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
        if growth or isolated_login or new_binding:
            profile["account_id"] = one(
                c,
                """INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,automation_enabled,connection_origin)
                VALUES(%s,%s,%s,%s,'pending',false,%s) RETURNING id""",
                (agent["tenant_id"], aid, profile["id"], platform, site_origin),
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


def public_research(aid):
    """A dedicated cookie-empty Profile, independent of every social account."""
    native=native_available()
    slot='native-public-research' if native else 'public-research'
    with tx(aid) as c:
        agent = require_agent(c,aid,True)
        profile = one(c,"SELECT * FROM lead.browser_profiles WHERE slot=%s",(slot,))
        if not profile:
            base = one(c,"SELECT * FROM lead.browser_profiles WHERE slot='default'")
            pid = uid()
            root = directory(aid,{"slot":slot,"id":pid})
            profile = one(c,"""INSERT INTO lead.browser_profiles(id,tenant_id,agent_id,provider,engine_version,
                sdk_version,config_ref,config_hash,storage_ref,key_material_ref,network_identity_id,slot)
                VALUES(%s,lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (pid,aid,'chromium' if native else base["provider"],'pending' if native else base["engine_version"],
                 'browser-use/0.13.14' if native else base["sdk_version"],str(root/"identity.json"),pid,
                 str(root/"storage"),base["key_material_ref"],base["network_identity_id"],slot))
            c.execute("""INSERT INTO lead.profile_leases(profile_id,tenant_id,agent_id,control_version)
                VALUES(%s,lead.ctx_tenant(),%s,%s)""",(pid,aid,agent["control_version"]))
    from .execution_routes import default_transport, register, resolve
    try:
        resolve(aid,profile_id=profile["id"])
    except ValueError as error:
        if str(error)!="execution_route_missing":
            raise
        register(aid,profile["id"],default_transport("public-research"))
    return profile
