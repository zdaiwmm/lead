"""A login is an ordinary account task; observation survives a closed frontend."""
import asyncio
import hashlib
import secrets
from contextlib import nullcontext
from datetime import timedelta

import httpx
from psycopg.types.json import Jsonb

from .account_intents import auth_scope
from .channel_contracts import SessionObservation
from .db import all_rows, emit, j, one, require_agent, tx
from .execution_routes import endpoint, register, resolve
from .growth.contracts import TaskSpec, Wait, WorkResult, utcnow
from .growth.store import submit_work
from .identity_store import auth_tx
from .security import internal_token


def observation_subject(platform, observed):
    if observed.get("platform", platform) != platform or not observed.get("external_account_id"):
        raise ValueError("connection_proof")
    stable = platform in ("telegram", "facebook", "youtube", "whatsapp")
    raw = observed["external_account_id"]
    return {"namespace": platform, "external_id": raw if stable else raw.casefold(),
            "id_kind": "stable" if stable else "handle", "aliases": [] if stable or raw==raw.casefold() else [raw]}


async def observe_identity(page,platform,session_ref):
    from .adapters import get_adapter
    from .adapters.core import (
        HumanVerificationRequired,
        LayoutChanged,
        NeedsAuth,
        check_challenge,
    )
    from .telegram import TelegramNeedsAuth
    try:
        await check_challenge(page)
        observed = await get_adapter(platform).account(page)
        subject = observation_subject(platform,observed)
        evidence = platform+":self-session:"+str(session_ref)+":"+hashlib.sha256(
            __import__("json").dumps(subject,sort_keys=True).encode()).hexdigest()
        return SessionObservation(status="authenticated",operating_subject=subject,evidence_refs=[evidence])
    except HumanVerificationRequired:
        return SessionObservation(status="challenge",owner_action="请在登录窗口完成平台验证")
    except (NeedsAuth,TelegramNeedsAuth):
        return SessionObservation(status="signed_out")
    except LayoutChanged:
        return SessionObservation(status="temporarily_unavailable")


async def discover_in_browser(actor, account, target_urls=None):
    """Only current-session self/management surfaces produce candidates; each candidate is inspected."""
    import re
    from urllib.parse import urljoin, urlsplit

    from .adapters import get_adapter
    from .adapters.catalog import SPECS, target_url
    from .adapters.core import ScopeChanged
    platform, external = account["platform"], account["external_account_id"]
    adapter = get_adapter(platform)
    self_urls = {
        "instagram": f"https://www.instagram.com/{external}/", "x": f"https://x.com/{external}/",
        "threads": f"https://www.threads.com/@{external}/", "tiktok": f"https://www.tiktok.com/@{external}/",
        "youtube": f"https://www.youtube.com/channel/{external}/", "linkedin": f"https://www.linkedin.com/in/{external}/",
    }
    candidates = [self_urls[platform]] if platform in self_urls else []
    for url in target_urls or []:
        candidates.append(target_url(platform,url))
    # Visible links in this authenticated session are candidates, never role
    # proof. Every candidate still passes the platform's target inspector.
    for href in await actor.page.locator("a[href]").evaluate_all("els=>els.filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().height).slice(0,100).map(e=>e.href)"):
        try:
            candidates.append(target_url(platform,href))
        except ValueError:
            pass
    for target in all_rows_for_account(actor.aid, account["id"]):
        candidates.append(target["canonical_url"])
    directory = {"reddit": "https://www.reddit.com/subreddits/mine/moderator/",
                 "pinterest": f"https://www.pinterest.com/{external}/_saved/"}.get(platform)
    if directory:
        await actor.page.goto(directory, wait_until="domcontentloaded", timeout=45000)
        own = await adapter.account(actor.page)
        if own["external_account_id"] != external:
            raise ScopeChanged("登录身份已改变")
        for href in await actor.page.locator("a[href]").evaluate_all("els=>els.slice(0,300).map(e=>e.href)"):
            parsed = urlsplit(urljoin(directory, href))
            match = re.fullmatch(r"/r/([A-Za-z0-9_]+)/?", parsed.path) if platform == "reddit" else re.fullmatch(r"/"+re.escape(external)+r"/([A-Za-z0-9_-]+)/?", parsed.path)
            if match and match[1] not in ("_saved", "_created"):
                try:
                    candidates.append(target_url(platform, parsed.geturl()))
                except ValueError:
                    continue
    results, gaps = [], []
    for url in list(dict.fromkeys(candidates))[:20]:
        try:
            target = await adapter.target(actor.page, url, external)
            results.append({**target, "acting_subject": external,
                            "evidence_refs": [f"{platform}:target-role:{actor.session}:"+hashlib.sha256(url.encode()).hexdigest()]})
        except ScopeChanged:
            continue
    if not results:
        gaps.append({"code": "target_discovery_unresolved", "responsibility": "product", "platform": platform,
                     "adapter_version": SPECS[platform].version})
    return {"targets": results, "missing_conditions": gaps}


def all_rows_for_account(aid, account_id):
    with tx(aid) as c:
        return all_rows(c, "SELECT canonical_url FROM lead.account_targets WHERE account_id=%s AND state='active'", (account_id,))


async def rpc(aid, profile_id, path, body=None, timeout=90):
    route = await asyncio.to_thread(resolve, aid, profile_id=profile_id)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(endpoint(route)+f"/agents/{aid}/"+path, json=body or {},
                                     headers={"Authorization": "Bearer "+internal_token()})
        response.raise_for_status()
        return response.json()


async def begin(aid, platform, *, account_id=None, new_account=False, site_url=None, login=None):
    from .account_profiles import for_platform
    from .domain import PLATFORMS
    from .execution_routes import default_transport
    profile = await asyncio.to_thread(for_platform, aid, platform, create=True, account_id=account_id,
                                      new_binding=new_account, site_url=site_url)
    with tx(aid) as c:
        pending=one(c,"""SELECT t.* FROM lead.takeover_sessions t JOIN lead.browser_sessions b ON b.id=t.browser_session_id
            WHERE t.profile_id=%s AND t.purpose='login' AND t.state='active' AND t.expires_at>now()
            AND b.ended_at IS NULL AND EXISTS(SELECT FROM lead.tasks w WHERE
                w.input#>>'{spec,payload,takeover_id}'=t.id::text
                AND w.input#>>'{spec,payload,connection_account_id}'=%s)
            ORDER BY t.created_at DESC LIMIT 1""",(profile['id'],str(profile['account_id'])))
        if pending:
            return connection_reply(pending,profile['account_id'],platform,native=profile['provider']=='chromium')
    try:
        await asyncio.to_thread(resolve, aid, profile_id=profile["id"])
    except ValueError as error:
        if str(error) != "execution_route_missing":
            raise
        # Installation chooses a transport, never the client or model. No runtime fallback.
        await asyncio.to_thread(register, aid, profile["id"], default_transport(platform))
    opened = await rpc(aid, profile["id"], "open", {"platform": platform, "account_id": str(profile["account_id"])})
    with tx(aid) as c:
        agent = require_agent(c, aid)
        if str(opened.get("profile_id")) != str(profile["id"]):
            raise ValueError("profile_changed")
        c.execute("""UPDATE lead.takeover_sessions t SET state='expired',ended_at=now() WHERE profile_id=%s
            AND state IN ('requested','active','ending') AND (expires_at<=now() OR EXISTS(SELECT FROM lead.tasks w
            WHERE w.input#>>'{spec,payload,takeover_id}'=t.id::text
            AND w.input#>>'{spec,payload,connection_account_id}'=%s))""", (profile["id"],str(profile['account_id'])))
        takeover = one(c, """INSERT INTO lead.takeover_sessions(tenant_id,agent_id,browser_session_id,profile_id,
            token_hash,control_version,purpose,state,expires_at,control_mode,connection_data,connection_account_id) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,'login','active',now()+interval '30 minutes',%s,%s,%s) RETURNING *""",
            (aid, opened["session_id"], profile["id"], hashlib.sha256(secrets.token_bytes(32)).hexdigest(), agent["control_version"],
             'automatic' if profile['provider']=='chromium' else 'owner',
             j({'site_url':site_url or next((url for name,_,url in PLATFORMS if name==platform),None)}),profile['account_id']))
        if login:
            if profile['provider']!='chromium':
                raise ValueError('automatic_login_requires_native_profile')
            from .connection_control import seal
            c.execute('UPDATE lead.takeover_sessions SET encrypted_login=%s WHERE id=%s',
                (seal(aid,takeover['id'],login),takeover['id']))
        submit_work(c, aid, TaskSpec(kind="account", purpose="connect", priority="control",
            payload={"takeover_id": str(takeover["id"]), "profile_id": str(profile["id"]),
                     "connection_account_id": str(profile["account_id"])},
            review_at=utcnow()+timedelta(minutes=30), dedupe_key="login:"+str(takeover["id"])))
    return connection_reply(takeover,profile['account_id'],platform,native=profile['provider']=='chromium')


def connection_reply(row,account_id,platform,*,native=False):
    return {'takeover_id':row['id'],'account_id':account_id,'platform':platform,
            'status':'connected' if row.get('completed_account_id') else 'pending',
            'automatic_connection':native,'control_mode':row['control_mode'],
            'challenge_version':row['challenge_version'],'owner_action':row['connection_data'].get('owner_action'),
            'expires_at':row['expires_at'],'managed_target_start':False,'automation':False}


def complete(aid, row, snapshot, observation):
    observed = SessionObservation.model_validate(observation)
    if observed.status != "authenticated" or not observed.operating_subject or not observed.evidence_refs:
        raise ValueError("connection_proof")
    subject = observed.operating_subject.model_dump(mode="json")
    with auth_tx() as c:
        auth_scope(c, aid)
        value = c.execute("SELECT lead.growth_complete_connection(%s,%s,%s,%s,%s,%s,%s,%s,%s) id",
            (aid, snapshot["id"], row["id"], snapshot["input_seq"], snapshot["route_generation"],
             snapshot["binding_version"], Jsonb(subject), subject["namespace"]+" · "+subject["external_id"][:64],
             Jsonb(observed.evidence_refs))).fetchone()
    return str(value["id"])


def connected(aid, account_id):
    """Idempotent outbox consumer. Discovery and business judgment are independent."""
    with tx(aid) as c:
        account = one(c, "SELECT * FROM lead.social_accounts WHERE id=%s AND state='connected'", (account_id,))
        if not account:
            return
        submit_work(c, aid, TaskSpec(kind="account", purpose="discover", account_id=account["id"],
            review_at=utcnow()+timedelta(hours=2), dedupe_key="targets:"+str(account["binding_version"])))
        emit(c, aid, "growth.wake", {"reason": "account_connected", "account_id": str(account_id),
                                    "binding_version": account["binding_version"]})


def _snapshot(aid, data):
    with tx(aid) as c:
        return one(c, """SELECT t.*,s.platform,s.binding_version,p.provider,r.generation route_generation FROM lead.takeover_sessions t
            JOIN lead.social_accounts s ON s.id=%s AND s.profile_id=t.profile_id
            JOIN lead.browser_profiles p ON p.id=t.profile_id
            JOIN lead.profile_execution_routes r ON r.profile_id=t.profile_id WHERE t.id=%s""",
            (data.connection_account_id, data.takeover_id))


async def execute_account(aid, row, data):
    if row["input"]["spec"]["purpose"] == "discover":
        return await discover(aid, row)
    if not data.takeover_id or not data.connection_account_id or not data.profile_id:
        raise ValueError("connection_input_missing")
    snapshot = await asyncio.to_thread(_snapshot, aid, data)
    if not snapshot:
        raise ValueError("execution_route_missing")
    if snapshot["completed_account_id"]:
        return WorkResult(outcome="completed", output={"account_id": str(snapshot["completed_account_id"])})
    if snapshot["state"] != "active" or snapshot["expires_at"] <= utcnow():
        with tx(aid) as c:
            c.execute("UPDATE lead.takeover_sessions SET state='expired',ended_at=coalesce(ended_at,now()),encrypted_login=NULL WHERE id=%s AND expires_at<=now()",(snapshot['id'],))
        return WorkResult(outcome="failed", failure_code="connection_expired")
    wait = Wait(reason="login_in_progress", producer="account_connection", recovery_action="recheck",
                check_at=utcnow()+timedelta(seconds=10), episode_root=row["id"], scope_ref=str(data.profile_id))
    if snapshot["input_inflight"] or snapshot["composing"] or (snapshot["last_input_at"] and snapshot["last_input_at"] > utcnow()-timedelta(seconds=2)):
        return WorkResult(outcome="waiting", wait=wait)
    if snapshot['provider']=='chromium' and snapshot['control_mode']=='automatic':
        try:
            observed=await rpc(aid,data.profile_id,'account/automatic',{'task_id':str(row['id']),
                'takeover':str(data.takeover_id),'account_id':str(data.connection_account_id)},timeout=180)
        except httpx.HTTPError:
            wait.reason='temporarily_unavailable'
            return WorkResult(outcome='waiting',wait=wait)
        if observed.get('control_mode')=='owner':
            observed={'status':'challenge','owner_action':observed.get('owner_action') or '请在原窗口完成本人步骤。'}
    else:
        observed = await rpc(aid, data.profile_id, "account/observe", {"platform": snapshot["platform"],
            "account_id": str(data.connection_account_id), "takeover": str(data.takeover_id)})
    observation = SessionObservation.model_validate(observed)
    if observation.status == "authenticated":
        import psycopg
        try:
            account_id = await asyncio.to_thread(complete, aid, row, snapshot, observation)
        except psycopg.errors.RaiseException as error:
            if "connection_changed" in str(error):
                return WorkResult(outcome="waiting", wait=wait)
            if "connection_identity_ambiguous" in str(error):
                wait.reason,wait.recovery_action,wait.responsibility = "identity_ambiguous","owner","owner"
                wait.escalation = "当前可见账号名与原绑定不同，尚不能证明是改名还是换号。如更换账号，请解除原绑定或连接另一个账号；原暂停与权限保持原状。"
                return WorkResult(outcome="waiting",wait=wait)
            raise
        return WorkResult(outcome="completed", output={"account_id": account_id, "connection_proof": observation.evidence_refs})
    wait.reason = observation.status
    if observation.status in ("challenge", "ambiguous"):
        wait.recovery_action = "owner"
        wait.responsibility = "owner"
        wait.escalation = observation.owner_action or "Complete the identity challenge in the original window."
    return WorkResult(outcome="waiting", wait=wait)


async def discover(aid, row):
    account_id = row["input"]["spec"]["account_id"]
    with tx(aid) as c:
        account = one(c, "SELECT * FROM lead.social_accounts WHERE id=%s", (account_id,))
    observed = await rpc(aid, account["profile_id"], "account/discover", {"platform": account["platform"], "account_id": str(account_id),
        'task_id':str(row['id']),
        "target_urls":row["input"]["spec"]["payload"].get("target_urls",[])})
    targets = await asyncio.to_thread(derive_targets,aid,account,observed.get('targets',[])[:20])
    if not targets and observed.get('continuation'):
        return WorkResult(outcome='waiting',checkpoint=observed['continuation'],
            wait=Wait(reason='bounded_browser_fragment',producer='account_connection',recovery_action='recheck',
                check_at=utcnow()+timedelta(seconds=15),episode_root=row['id'],scope_ref=str(account['profile_id'])))
    return WorkResult(outcome="completed", output={"targets": targets, "missing_conditions": observed.get("missing_conditions", []),
                                                  "knowledge_progress": any(value.get('knowledge_progress',True) for value in targets)})


def derive_targets(aid,account,observations):
    with tx(aid) as c:
        return [derive_target(aid,account,observed,connection=c) for observed in observations]


def derive_target(aid, account, observed,*,connection=None):
    if observed.get('browser_method'):
        return derive_browser_target(aid,account,observed,connection=connection)
    from .adapters.catalog import SPECS
    spec = SPECS[account["platform"]]
    if not observed.get("evidence_refs") or observed.get("acting_subject") != account["external_account_id"]:
        raise ValueError("target_not_permitted")
    with nullcontext(connection) if connection is not None else tx(aid) as c:
        current = one(c, "SELECT * FROM lead.social_accounts WHERE id=%s FOR SHARE", (account["id"],))
        intent = one(c, "SELECT * FROM lead.growth_read_intent(%s)", (account["id"],))
        if not current or current["binding_version"] != account["binding_version"] or not intent or intent["desired"] != "running":
            raise ValueError("account_changed")
        if observed["external_target_id"] in intent["scope"].get("excluded_targets", []):
            raise ValueError("target_not_permitted")
        if intent["scope"].get("target_scope")=="existing_grants" and observed["external_target_id"] not in intent["scope"].get("target_ids",[]):
            raise ValueError("target_not_permitted")
        allowed = sorted(set(observed["verified_permissions"]) & {"publish", "upload_media"})
        if "publish" not in allowed or observed.get("access_mode") == "ordinary_member":
            raise ValueError("target_not_permitted")
        target = one(c, """INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,
            canonical_url,display_name,verified_permissions,state) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,%s,'active')
            ON CONFLICT(account_id,target_type,external_target_id) DO UPDATE SET
            verified_permissions=account_targets.verified_permissions || EXCLUDED.verified_permissions,state='active' RETURNING *""",
            (aid, account["id"], observed.get("target_type", spec.target_type), observed["external_target_id"],
             observed["canonical_url"], observed["display_name"], j({"actions": allowed, "adapter_version": spec.version,
             "role_evidence": observed["evidence_refs"], "binding_version": account["binding_version"]})))
        grant = one(c, "SELECT * FROM lead.authorization_grants WHERE target_id=%s AND revoked_at IS NULL", (target["id"],))
        if not grant:
            grant = one(c, """INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,limits,granted_by)
              SELECT lead.ctx_tenant(),%s,%s,coalesce(max(version),0)+1,%s,%s,'account_intent' FROM lead.authorization_grants WHERE target_id=%s RETURNING *""",
              (aid, target["id"], j(allowed), j({"intent_revision": intent["revision"], "account_id": str(account["id"]),
               "binding_version": account["binding_version"], "formats": list(spec.formats), "publish_per_day": intent["scope"].get("publish_per_day")}), target["id"]))
        return {"target_id": str(target["id"]), "grant_id": str(grant["id"])}


def derive_browser_target(aid,account,observed,*,connection=None):
    from .browser_execution import validate_method
    method=observed['browser_method']
    operations=method.get('operations',[]) if isinstance(method,dict) else []
    if (not observed.get('evidence_refs') or observed.get('acting_subject')!=account['external_account_id']
            or len(operations)!=1 or observed.get('access_mode')!='ordinary_member'):
        raise ValueError('target_not_permitted')
    operation=operations[0]
    method=validate_method(method,account_id=account['id'],binding_version=account['binding_version'],
        target_url=observed['canonical_url'],operation=operation)
    with nullcontext(connection) if connection is not None else tx(aid) as c:
        agent=require_agent(c,aid,True)
        current=one(c,'SELECT * FROM lead.social_accounts WHERE id=%s FOR SHARE',(account['id'],))
        intent=one(c,'SELECT * FROM lead.growth_read_intent(%s)',(account['id'],))
        if (not current or current['state']!='connected' or current['binding_version']!=account['binding_version']
                or not intent or intent['desired']!='running' or intent['scope'].get('target_scope') not in
                    ('authenticated_permitted_targets','existing_grants')
                or observed['external_target_id'] in intent['scope'].get('excluded_targets',[])
                or intent['scope'].get('target_scope')=='existing_grants' and observed['external_target_id'] not in intent['scope'].get('target_ids',[])):
            raise ValueError('target_not_permitted')
        producer=observed.get('producer_scope') or {}
        work=one(c,'SELECT * FROM lead.tasks WHERE id=%s FOR SHARE',(producer.get('task_id'),))
        runtime=(work or {}).get('result',{}).get('runtime',{})
        from datetime import datetime
        if (not work or work['state']!='running' or work['type']!='growth.account'
                or work['input']['spec'].get('purpose')!='discover'
                or str(work['input']['spec'].get('account_id'))!=str(account['id'])
                or work['input'].get('binding_version')!=current['binding_version']
                or work['input'].get('task_generation')!=producer.get('task_generation')
                or producer.get('control_version')!=agent['control_version']
                or runtime.get('control_version')!=agent['control_version']
                or not producer.get('attempt_token') or runtime.get('attempt_token')!=producer['attempt_token']
                or not runtime.get('lease_until') or datetime.fromisoformat(runtime['lease_until'])<=utcnow()
                or producer.get('intent_revision')!=intent['revision']):
            raise ValueError('native_method_observation_expired')
        from .channel_contracts import ExecutionRef
        execution=ExecutionRef.model_validate(producer.get('execution_ref'))
        route=one(c,'SELECT * FROM lead.profile_execution_routes WHERE profile_id=%s',(current['profile_id'],))
        if not route or execution.profile_id!=current['profile_id'] or any(str(route[key])!=str(getattr(execution,field)) for key,field in
                (('generation','route_generation'),('executor_ref','executor_ref'),('transport_ref','transport_ref'))):
            raise ValueError('native_method_observation_expired')
        endpoint(execution)
        from uuid import UUID
        try:
            browser_session=UUID(str(producer.get('browser_session_id')))
        except (ValueError,TypeError):
            raise ValueError('native_method_observation_expired') from None
        browser_epoch=producer.get('browser_epoch')
        if type(browser_epoch) is not int or browser_epoch<1:
            raise ValueError('native_method_observation_expired')
        # Match the manager heartbeat/close order: profile lease before session.
        # A joined locking SELECT leaves planner order unspecified.
        lease=one(c,"""SELECT profile_id FROM lead.profile_leases WHERE profile_id=%s AND epoch=%s
            AND state='held' AND expires_at>now() FOR SHARE""",(current['profile_id'],browser_epoch))
        session=one(c,"""SELECT id FROM lead.browser_sessions WHERE id=%s AND profile_id=%s AND epoch=%s
            AND state='ready' AND ended_at IS NULL FOR SHARE""",(browser_session,current['profile_id'],browser_epoch)) if lease else None
        if not lease or not session:
            raise ValueError('native_method_observation_expired')
        old=one(c,'SELECT * FROM lead.account_targets WHERE account_id=%s AND target_type=%s AND external_target_id=%s FOR UPDATE',
            (account['id'],'web',observed['external_target_id']))
        permissions=old['verified_permissions'] if old else {}
        if (not isinstance(permissions,dict) or not isinstance(permissions.get('browser_methods',{}),dict)
                or not isinstance(permissions.get('browser_method_producers',{}),dict)):
            raise ValueError('browser_method_scope_changed')
        previous=permissions.get('browser_methods',{}).get(operation)
        def facts(value):
            if not isinstance(value,dict):
                return value
            return {key:facts(item) if isinstance(item,dict) else item for key,item in value.items()
                if key not in ('evidence_refs','checked_at')}
        learned=previous is None or facts(previous)!=facts(method)
        if not learned:
            method=previous
        methods={**permissions.get('browser_methods',{}),operation:method}
        allowed=sorted(methods)
        from .browser_actions import method_hash
        permissions['browser_method_producers']={**permissions.get('browser_method_producers',{}),operation:{**producer,'method_hash':method_hash(method)}}
        permissions.update(browser_methods=methods,actions=allowed,access_mode='ordinary_member',binding_version=account['binding_version'])
        target=one(c,"""INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,
            canonical_url,display_name,verified_permissions,state) VALUES(lead.ctx_tenant(),%s,%s,'web',%s,%s,%s,%s,'active')
            ON CONFLICT(account_id,target_type,external_target_id) DO UPDATE SET verified_permissions=EXCLUDED.verified_permissions,
            canonical_url=EXCLUDED.canonical_url,display_name=EXCLUDED.display_name,state='active' RETURNING *""",
            (aid,account['id'],observed['external_target_id'],observed['canonical_url'],observed['display_name'],j(permissions)))
        grant=one(c,'SELECT * FROM lead.authorization_grants WHERE target_id=%s AND revoked_at IS NULL FOR UPDATE',(target['id'],))
        limits={'intent_revision':intent['revision'],'account_id':str(account['id']),'binding_version':account['binding_version'],
            'formats':['text'],'publish_per_day':intent['scope'].get('publish_per_day')}
        if grant and (grant['allowed_actions']!=allowed or grant['limits']!=limits):
            c.execute('UPDATE lead.authorization_grants SET revoked_at=now() WHERE id=%s',(grant['id'],))
            grant=None
        if not grant:
            grant=one(c,"""INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,limits,granted_by)
                SELECT lead.ctx_tenant(),%s,%s,coalesce(max(version),0)+1,%s,%s,'account_intent'
                FROM lead.authorization_grants WHERE target_id=%s RETURNING *""",(aid,target['id'],j(allowed),j(limits),target['id']))
        return {'target_id':str(target['id']),'grant_id':str(grant['id']),'operation':operation,'knowledge_progress':learned}
