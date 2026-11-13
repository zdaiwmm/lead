"""Owner-approved exact public frames on an original native connection/task.

An iframe URL is a candidate, never permission. Approved frames are forced into
a credentialless, opaque sandbox; the model cannot navigate, type or submit in
them. Existing profile identity/storage and authentication endpoints are refused.
All durable facts live on the original ConnectionRef, with no separate ledger.
"""

import asyncio
import copy
import hashlib
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from .browser_execution import origin
from .db import j, one, require_agent, tx
from .growth.contracts import utcnow

REASONS = {'account_origin_conflict', 'profile_storage_present', 'storage_unverifiable',
    'authentication_dependency', 'source_changed', 'approval_expired', 'isolation_unsupported',
    'identity_required', 'owner_step_pending'}
LIFETIME = timedelta(minutes=15)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def authentication_resource(url):
    try:
        parsed=urlsplit(url)
    except ValueError:
        return True
    return bool(re.search(r'/(?:login|signin|sign-in|logout|auth|authorize|oauth2?|sso|cas|saml|session|callback|token)(?:[/._-]|$)',parsed.path,re.I)
        or any(re.search(r'auth|token|secret|password|credential|session|cookie|ticket|saml|assertion|(?:^|[_-])(?:key|sig|signature|jwt|code|state)(?:$|[_-])',
            name,re.I) for name,_ in parse_qsl(parsed.query,keep_blank_values=True)))


def refused_address(url):
    try:
        parsed=urlsplit(url)
        return canonical_origin(url)+parsed.path
    except ValueError:
        return 'about:blank'


async def root_facts(runtime, scope, page):
    await asyncio.to_thread(runtime.current_scope, scope)
    token = copy.deepcopy(runtime.gate.documents.get(scope['target_id']))
    if not token or page.url != token[2] or origin(page.url) != origin(scope['origins'][0]):
        raise ValueError('frame_source_changed')
    facts = await page.evaluate('''()=>({parent_url:location.href,supported:'credentialless' in HTMLIFrameElement.prototype,
        secure:isSecureContext,frames:Array.from(document.querySelectorAll('iframe[src]')).map((node,index)=>({index,
            resource_url:new URL(node.getAttribute('src'),node.baseURI).href,
            sandbox:node.getAttribute('sandbox'),credentialless:node.credentialless===true,
            referrerpolicy:node.getAttribute('referrerpolicy'),
            name:node.getAttribute('name')||'',title:(node.getAttribute('title')||'').slice(0,200)}))})''')
    if token != runtime.gate.documents.get(scope['target_id']) or facts['parent_url'] != page.url:
        raise ValueError('frame_source_changed')
    await asyncio.to_thread(runtime.current_scope, scope)
    return facts, token


def conflict(c, profile_id, url, *, account_id):
    target = origin(url)
    accounts = c.execute('SELECT id,connection_origin FROM lead.social_accounts WHERE profile_id=%s', (profile_id,)).fetchall()
    for account in accounts:
        if account.get('connection_origin') and origin(account['connection_origin']) == target:
            return 'account_origin_conflict'
    rows = c.execute('''SELECT connection_data FROM lead.takeover_sessions WHERE profile_id=%s AND purpose='login' ''', (profile_id,)).fetchall()
    for row in rows:
        endpoints = row['connection_data'].get('authentication_endpoints', [])
        flow = row['connection_data'].get('auth_flow', {})
        if any(origin(value) == target for value in endpoints + flow.get('urls', [])):
            return 'authentication_dependency'
    return None


async def known_conflict(runtime, scope, url):
    """Reject known identity/storage conflicts; never claim the profile is empty.

    The positive boundary is the later actual opaque/credentialless partition.
    No profile state is loaded, cleared, or used as the public frame identity.
    """
    target = origin(url)
    with tx(runtime.actor.aid) as c:
        reason = conflict(c, runtime.actor.profile['id'], url, account_id=scope.get('account_id'))
    if reason:
        return reason
    if await runtime.actor.context.cookies(url):
        return 'profile_storage_present'
    try:
        page_session = next(sid for sid, info in runtime.gate.targets.items() if info['targetId'] == scope['target_id'])
        quota = await runtime.gate.send('Storage.getUsageAndQuota', {'origin': canonical_origin(url)}, page_session)
        if quota['usage'] != 0:
            return 'profile_storage_present'
        for version in runtime.gate.worker_versions.values():
            if version.get('scriptURL') and origin(version['scriptURL']) == target and version.get('status') != 'redundant':
                return 'profile_storage_present'
        for registration in runtime.gate.worker_registrations.values():
            if not registration.get('isDeleted') and origin(registration['scopeURL']) == target:
                return 'profile_storage_present'
        return None
    except Exception:
        return 'storage_unverifiable'


def canonical_origin(url):
    scheme, host, port = origin(url)
    host = '['+host+']' if ':' in host else host
    return scheme+'://'+host+(':'+str(port) if port != (443 if scheme == 'https' else 80) else '')


def _source_scope(c, runtime, scope):
    if not scope.get('reconcile_action_id'):
        return scope
    # A later readonly claim may run after business pause/revocation. Its
    # dependency identity comes from the original Action, never caller fields
    # or a new running business intent. Current Task/instance checks stay on
    # the actual page scope, not this private source-lookup projection.
    action=one(c,'''SELECT * FROM lead.actions WHERE id=%s AND task_id=%s
        AND state IN ('unknown','dispatching','confirmed')''',
        (scope['reconcile_action_id'],scope['task_id']))
    intent=(action or {}).get('intent') or {}
    method=intent.get('browser_method') or {}
    reference=intent.get('execution_ref') or {}
    if (not action or scope.get('connection_ref') or reference!=runtime.execution_ref
            or str(reference.get('profile_id'))!=str(runtime.actor.profile['id'])
            or scope['origins']!=[method.get('target_url')]):
        raise ValueError('original_frame_action_changed')
    identity={'account_id':method['account_id'],'binding_version':intent['binding_version'],
        'intent_revision':intent['intent_revision'],'control_version':action['expected_control_version']}
    if any(scope.get(key) is not None and str(scope[key])!=str(identity[key])
            for key in ('account_id','binding_version','intent_revision')):
        raise ValueError('original_frame_action_changed')
    return {**scope,**identity}


def connection_sources(c, runtime, scope):
    scope=_source_scope(c,runtime,scope)
    if scope.get('connection_ref'):
        connection = one(c, 'SELECT * FROM lead.takeover_sessions WHERE id=%s', (scope['connection_ref'],))
    else:
        connection = one(c, '''SELECT * FROM lead.takeover_sessions WHERE completed_account_id=%s
            AND profile_id=%s AND connection_data ? 'read_dependencies' ORDER BY ended_at DESC NULLS LAST LIMIT 1''',
            (scope.get('account_id'), runtime.actor.profile['id']))
    if not connection:
        return []
    valid = []
    for source in connection['connection_data'].get('read_dependencies', []):
        if source['state'] != 'approved' or datetime.fromisoformat(source['expires_at']) <= utcnow():
            continue
        if (source.get('profile_id')!=str(runtime.actor.profile['id']) or source.get('execution_ref')!=runtime.execution_ref):
            continue
        authority = source.get('bound') if not scope.get('connection_ref') else source
        if not authority or any(str(authority.get(key)) != str(value) for key, value in (
                ('account_id', scope.get('account_id')), ('control_version', scope['control_version']),
                ('browser_session_id', runtime.actor.session), ('browser_epoch', runtime.actor.epoch))):
            continue
        if scope.get('connection_ref'):
            if source.get('document')!=runtime.gate.documents.get(scope['target_id']):
                continue
            phase = source.get('automatic_phase') if scope.get('control_mode') == 'automatic' else source['challenge_version']
            if phase != connection['challenge_version']:
                continue
        elif (authority.get('binding_version') != scope['binding_version']
              or authority.get('intent_revision') != scope['intent_revision']):
            continue
        valid.append(source)
    return valid


async def candidates(controller):
    runtime, scope, page = controller.runtime, controller.lease, controller.page
    task, connection, account = await asyncio.to_thread(controller.snapshot)
    if connection['control_mode'] != 'owner' or connection['state'] != 'active' or not scope:
        raise ValueError('frame_owner_step_pending')
    if connection['input_inflight'] or connection['composing']:
        raise ValueError('frame_owner_input_pending')
    # Owner metadata reads only the original first-party DOM, even after a
    # previously granted child role expires. This never returns child content.
    metadata_scope={**scope,'frame_roles':[]}
    facts, document = await root_facts(runtime, metadata_scope, page)
    old = {row['candidate_id']: row for row in connection['connection_data'].get('read_dependencies', [])}
    rows = []
    for frame in facts['frames'][:12]:
        denied=None
        try:
            if origin(frame['resource_url']) == origin(facts['parent_url']) or urlsplit(frame['resource_url']).fragment:
                continue
            await controller.actor.forward_proxy.validate_url(frame['resource_url'])
        except ValueError:
            denied='isolation_unsupported'
        key = digest([facts['parent_url'], frame['resource_url']])
        item = old.get(key)
        if not item:
            reason=denied or ('authentication_dependency' if authentication_resource(frame['resource_url']) else None)
            reason=reason or ('isolation_unsupported' if urlsplit(frame['resource_url']).query else None)
            reason=reason or ('isolation_unsupported' if not facts['supported'] or not facts['secure'] else await known_conflict(runtime, scope, frame['resource_url']))
            item = {'candidate_id': key, 'parent_url': facts['parent_url'], 'resource_url': frame['resource_url'],
                'role': 'read_only_frame', 'state': 'refused' if reason else 'pending', 'reason': reason,
                'expires_at': min(connection['expires_at'], utcnow()+LIFETIME).isoformat(), 'document': document,
                'original_attributes': {key: frame.get(key) for key in ('sandbox', 'credentialless', 'referrerpolicy','name', 'title')},
                'account_id': str(account['id']), 'control_version': connection['control_version'],
                'profile_id':str(controller.actor.profile['id']),'execution_ref':copy.deepcopy(runtime.execution_ref),
                'claim':copy.deepcopy(scope.get('claim')),
                'task_id': controller.task_id, 'task_generation': task['input']['task_generation'],
                'browser_session_id': str(controller.actor.session), 'browser_epoch': controller.actor.epoch,
                'challenge_version': connection['challenge_version']}
            if reason in ('authentication_dependency','isolation_unsupported'):
                item['resource_url']=refused_address(item['resource_url'])
                item['original_url_hash']=digest(frame['resource_url'])
        elif datetime.fromisoformat(item['expires_at']) <= utcnow():
            item = {**item, 'state': 'refused', 'reason': 'approval_expired'}
        elif item['challenge_version']!=connection['challenge_version'] or item['document']!=document:
            item={**item,'state':'refused','reason':'source_changed'}
        rows.append(item)
    after, token = await root_facts(runtime, metadata_scope, page)
    if after != facts or token != document:
        raise ValueError('frame_source_changed')
    with tx(controller.actor.aid) as c:
        agent=require_agent(c,controller.actor.aid,True)
        current = one(c, "SELECT * FROM lead.takeover_sessions WHERE id=%s FOR UPDATE", (connection['id'],))
        if (current['challenge_version'] != connection['challenge_version'] or current['input_seq'] != connection['input_seq']
                or current['control_version']!=agent['control_version']
                or current['control_mode'] != 'owner' or current['state'] != 'active' or current['input_inflight']):
            raise ValueError('frame_owner_step_pending')
        c.execute('UPDATE lead.takeover_sessions SET connection_data=connection_data || %s WHERE id=%s',
            (j({'read_dependencies': rows}), connection['id']))
    proofs={row.get('source_proof') for row in rows if row['state']=='approved'}
    scope['frame_roles']=[role for role in scope.get('frame_roles',[]) if role['source_proof'] in proofs
        and datetime.fromisoformat(role['expires_at'])>utcnow()]
    return public_reply(connection, rows)


def public_reply(connection, rows):
    return {'connection_id': str(connection['id']), 'challenge_version': connection['challenge_version'],
            'candidates': [{key: row.get(key) for key in ('candidate_id', 'resource_url', 'parent_url', 'role', 'expires_at', 'state', 'reason')} for row in rows]}


async def decide(controller, args):
    await candidates(controller)
    _, connection, _ = await asyncio.to_thread(controller.snapshot)
    if args.get('challenge_version') != connection['challenge_version'] or args.get('decision') not in ('approve', 'refuse'):
        raise ValueError('connection_challenge_changed')
    rows = copy.deepcopy(connection['connection_data']['read_dependencies'])
    selected = next((row for row in rows if row['candidate_id'] == args.get('candidate_id')), None)
    if not selected or selected['state'] != 'pending':
        raise ValueError('frame_source_changed')
    runtime, scope, page = controller.runtime, controller.lease, controller.page
    facts, token = await root_facts(runtime, scope, page)
    matches = [frame for frame in facts['frames'] if frame['resource_url'] == selected['resource_url']]
    if len(matches) != 1 or token != selected['document'] or facts['parent_url'] != selected['parent_url']:
        raise ValueError('frame_source_changed')
    element=await page.locator('iframe[src]').nth(matches[0]['index']).element_handle(timeout=500)
    root=await page.evaluate_handle('document.documentElement')
    reason = await known_conflict(runtime, scope, selected['resource_url']) if args['decision'] == 'approve' else None
    current,current_token=await root_facts(runtime,scope,page)
    if current!=facts or current_token!=token:
        await element.dispose()
        await root.dispose()
        raise ValueError('frame_source_changed')
    selected.update(state='refused' if reason or args['decision'] == 'refuse' else 'approved', reason=reason)
    if selected['state'] == 'approved':
        selected['source_proof'] = digest(selected)
    try:
        with tx(controller.actor.aid) as c:
            agent=require_agent(c, controller.actor.aid, True)
            changed = c.execute('''UPDATE lead.takeover_sessions SET connection_data=connection_data || %s WHERE id=%s
                AND state='active' AND control_mode='owner' AND control_version=%s AND challenge_version=%s AND input_seq=%s
                AND NOT input_inflight AND NOT composing AND (last_input_at IS NULL OR last_input_at<now()-interval '2 seconds')''',
                (j({'read_dependencies': rows}), connection['id'], agent['control_version'], connection['challenge_version'], connection['input_seq'])).rowcount
            if changed != 1:
                raise ValueError('connection_challenge_changed')
        if selected['state'] == 'approved':
            current,current_token=await root_facts(runtime,scope,page)
            if current!=facts or current_token!=token:
                raise ValueError('frame_source_changed')
            # Hold the actual observed element and original document root. A
            # replaced nth iframe, same-URL reload or changed src cannot receive
            # an old approval. One renderer call validates then installs flags.
            await element.evaluate('''(node,[expected,root])=>{if(!node.isConnected || node.ownerDocument!==document ||
                document.documentElement!==root || location.href!==expected.parent ||
                node!==document.querySelectorAll('iframe[src]')[expected.index] || node.src!==expected.url ||
                node.getAttribute('sandbox')!==expected.sandbox || node.credentialless!==expected.credentialless ||
                node.getAttribute('referrerpolicy')!==expected.referrerpolicy)
                    throw Error('frame_source_changed');
                node.setAttribute('referrerpolicy','no-referrer');node.setAttribute('sandbox','');node.credentialless=true;node.src=expected.url}''',
                [{'parent':selected['parent_url'],'index':matches[0]['index'],'url':selected['resource_url'],
                  'sandbox':matches[0]['sandbox'],'credentialless':matches[0]['credentialless'],
                  'referrerpolicy':matches[0].get('referrerpolicy')},root])
            after,after_token=await root_facts(runtime,scope,page)
            after_frame=[frame for frame in after['frames'] if frame['resource_url']==selected['resource_url']]
            if (after_token!=token or after['parent_url']!=facts['parent_url'] or len(after_frame)!=1
                    or after_frame[0]['sandbox']!='' or after_frame[0]['credentialless'] is not True
                    or after_frame[0].get('referrerpolicy')!='no-referrer'):
                raise ValueError('frame_source_changed')
    except Exception as error:
        if selected['state']=='approved':
            await asyncio.to_thread(refuse_source,runtime,scope,selected,'source_changed')
        raise ValueError('frame_source_changed') from error
    finally:
        await element.dispose()
        await root.dispose()
    return public_reply(connection, rows)


async def prepare(runtime, scope, page):
    lock=runtime.frame_role_locks.setdefault(scope['target_id'],asyncio.Lock())
    async with lock:
        return await _prepare(runtime,scope,page)


async def _prepare(runtime, scope, page):
    """Derive short-lived frame roles from current DOM and canonical owner proof."""
    if (not (scope.get('account_id') or scope.get('reconcile_action_id'))
            or scope.get('kind') == 'identity_probe' or scope.get('internal_parent')):
        return
    with tx(runtime.actor.aid) as c:
        sources = connection_sources(c, runtime, scope)
    if not sources:
        return
    facts, document = await root_facts(runtime, scope, page)
    scope['frame_roles'] = [role for role in scope.get('frame_roles', []) if role['document'] == document
        and any(frame['resource_url']==role['resource_url'] for frame in facts['frames'])]
    roles = scope.setdefault('frame_roles', [])
    for source in sources:
        matches = [frame for frame in facts['frames'] if frame['resource_url'] == source['resource_url']]
        if len(matches) != 1 or facts['parent_url'] != source['parent_url'] or not facts['secure'] or not facts['supported']:
            continue
        frame = matches[0]
        existing = next((role for role in roles if role['source_proof'] == source['source_proof'] and role['document'] == document), None)
        if existing:
            if frame['sandbox'] != '' or frame['credentialless'] is not True or frame.get('referrerpolicy')!='no-referrer':
                roles.remove(existing)
                await asyncio.to_thread(refuse_source,runtime,scope,existing,'source_changed')
                raise ValueError('frame_isolation_removed')
            # A new canonical task claim re-derives this technical lease from
            # the still-valid owner source; the old claim is never adopted.
            existing.update(task_id=scope['task_id'],task_generation=scope['task_generation'],
                claim=copy.deepcopy(scope.get('claim')),generation=scope['generation'])
            continue
        element=await page.locator('iframe[src]').nth(frame['index']).element_handle(timeout=500)
        root=await page.evaluate_handle('document.documentElement')
        role=None
        try:
            if await known_conflict(runtime, scope, source['resource_url']):
                raise ValueError('frame_profile_storage_conflict')
            current,current_document=await root_facts(runtime,scope,page)
            if current!=facts or current_document!=document:
                raise ValueError('frame_source_changed')
            role = {'source_proof': source['source_proof'], 'candidate_id': source['candidate_id'],
                'resource_url': source['resource_url'], 'parent_url': source['parent_url'], 'document': document,
                'element_index': frame['index'], 'activation_sequence': runtime.gate.network_sequence,
                'expires_at': min(datetime.fromisoformat(source['expires_at']), utcnow()+timedelta(seconds=180)).isoformat(),
                'task_id': scope['task_id'], 'task_generation': scope['task_generation'],
                'claim': copy.deepcopy(scope.get('claim')), 'generation': scope['generation']}
            roles.append(role)
            await element.evaluate('''(node,[expected,root])=>{if(!node.isConnected || node.ownerDocument!==document ||
                document.documentElement!==root || location.href!==expected.parent ||
                node!==document.querySelectorAll('iframe[src]')[expected.index] || node.src!==expected.url ||
                node.getAttribute('sandbox')!==expected.sandbox || node.credentialless!==expected.credentialless ||
                node.getAttribute('referrerpolicy')!==expected.referrerpolicy)
                    throw Error('frame_source_changed');
                node.setAttribute('referrerpolicy','no-referrer');node.setAttribute('sandbox','');node.credentialless=true;node.src=expected.url}''',
                [{'parent':source['parent_url'],'index':frame['index'],'url':source['resource_url'],
                  'sandbox':frame['sandbox'],'credentialless':frame['credentialless'],
                  'referrerpolicy':frame.get('referrerpolicy')},root])
            after,after_document=await root_facts(runtime,scope,page)
            if after_document!=document or after['parent_url']!=facts['parent_url']:
                raise ValueError('frame_source_changed')
        except Exception:
            if role in roles:
                roles.remove(role)
            raise
        finally:
            await element.dispose()
            await root.dispose()


def canonical_roles(c, runtime, scope):
    sources = {row['source_proof']: row for row in connection_sources(c, runtime, scope)}
    roles = []
    for role in scope.get('frame_roles', []):
        source = sources.get(role['source_proof'])
        if (not source or datetime.fromisoformat(role['expires_at']) <= utcnow()
                or any(role.get(key) != scope.get(key) for key in ('task_id', 'task_generation', 'claim', 'generation'))
                or any(role[key] != source[key] for key in ('resource_url', 'parent_url', 'candidate_id'))):
            raise ValueError('frame_source_expired')
        if conflict(c,runtime.actor.profile['id'],role['resource_url'],account_id=scope.get('account_id')):
            raise ValueError('frame_account_origin_conflict')
        roles.append(role)
    return roles


def refuse_source(runtime,scope,role,reason):
    if reason not in REASONS:
        raise ValueError('frame_refusal_reason_invalid')
    with tx(runtime.actor.aid) as c:
        require_agent(c,runtime.actor.aid,True)
        scope=_source_scope(c,runtime,scope)
        rows=c.execute('''SELECT id,connection_data FROM lead.takeover_sessions WHERE profile_id=%s
            AND (id::text=%s OR completed_account_id::text=%s) AND connection_data ? 'read_dependencies' FOR UPDATE''',
            (runtime.actor.profile['id'],scope.get('connection_ref'),scope.get('account_id'))).fetchall()
        for row in rows:
            sources=copy.deepcopy(row['connection_data']['read_dependencies'])
            changed=False
            for source in sources:
                if source.get('source_proof')==role['source_proof'] and source['state']=='approved':
                    source.update(state='refused',reason=reason)
                    changed=True
            if changed:
                c.execute('UPDATE lead.takeover_sessions SET connection_data=connection_data || %s WHERE id=%s',
                    (j({'read_dependencies':sources}),row['id']))


async def request(runtime, scope, target, request):
    """Only an exact isolated-frame GET/HEAD can use an approved dependency."""
    if request['method'] not in ('GET', 'HEAD') or target.get('type') == 'service_worker':
        return None
    entry = runtime.pages.get(scope['target_id'])
    if not entry:
        return None
    await prepare(runtime, scope, entry['page'])
    with tx(runtime.actor.aid) as c:
        roles = canonical_roles(c, runtime, scope)
    meta = request.get('_native_network') or {}
    for role in roles:
        if (request['url'] != role['resource_url'] or role['document'] != runtime.gate.documents.get(scope['target_id'])
                or runtime.gate.network.get((meta.get('network_id'), meta.get('session')), {}).get('_lead_sequence', 0) <= role['activation_sequence']):
            continue
        frames, token = await root_facts(runtime, scope, entry['page'])
        matches = [frame for frame in frames['frames'] if frame['resource_url'] == role['resource_url']]
        if (token != role['document'] or len(matches) != 1 or matches[0]['sandbox'] != ''
                or matches[0]['credentialless'] is not True or matches[0].get('referrerpolicy')!='no-referrer'):
            raise ValueError('frame_isolation_removed')
        root_session = next(sid for sid, info in runtime.gate.targets.items() if info['targetId'] == scope['target_id'])
        owner = await runtime.gate.send('DOM.getFrameOwner', {'frameId': meta['frame_id']}, root_session)
        resolved = await runtime.gate.send('DOM.resolveNode', {'backendNodeId': owner['backendNodeId']}, root_session)
        identifier = resolved['object']['objectId']
        try:
            actual = await runtime.gate.send('Runtime.callFunctionOn', {'objectId': identifier,
                'functionDeclaration': '''function(value){return this===document.querySelectorAll('iframe[src]')[value.index] &&
                    this.src===value.url && this.getAttribute('sandbox')==='' && this.credentialless===true &&
                    this.getAttribute('referrerpolicy')==='no-referrer'}''',
                'arguments': [{'value': {'index': role['element_index'], 'url': role['resource_url']}}], 'returnByValue': True}, root_session)
            if actual.get('result', {}).get('value') is not True:
                return None
        finally:
            await runtime.gate.send('Runtime.releaseObject', {'objectId': identifier}, root_session)
        if await runtime.actor.context.cookies(role['resource_url']):
            await asyncio.to_thread(refuse_source,runtime,scope,role,'profile_storage_present')
            raise ValueError('frame_profile_storage_conflict')
        role.update(frame_target=target['targetId'], frame_id=meta['frame_id'])
        async def refused_response(status):
            await asyncio.to_thread(refuse_source,runtime,scope,role,
                'identity_required' if status in (401,403) else 'isolation_unsupported')
        return {'kind': 'read', 'public_frame': True, 'cookie_header': '', 'frame_role': role,
            'refuse_response':refused_response}
    return None


async def observation(runtime, scope, page):
    """Prove actual anonymous/opaque child state before any SDK/DOM output."""
    await prepare(runtime, scope, page)
    with tx(runtime.actor.aid) as c:
        roles = canonical_roles(c, runtime, scope)
    origins = list(scope['origins'])
    proven_children=[]
    if scope.get('connection_ref') and not roles:
        # Connection.start returns its root lease before the fixed preflight
        # hands an unapproved foreign frame to the owner. No SDK observation
        # succeeds with that frame outside the returned original origin.
        return origins
    for role in roles:
        if role['document'] != runtime.gate.documents.get(scope['target_id']):
            raise ValueError('frame_source_changed')
        if await runtime.actor.context.cookies(role['resource_url']):
            raise ValueError('frame_profile_storage_conflict')
        node = page.locator('iframe[src]').nth(role['element_index'])
        attrs = await node.evaluate("node=>({src:node.src,sandbox:node.getAttribute('sandbox'),credentialless:node.credentialless===true,referrerpolicy:node.getAttribute('referrerpolicy')})")
        if attrs != {'src': role['resource_url'], 'sandbox': '', 'credentialless': True,'referrerpolicy':'no-referrer'}:
            await asyncio.to_thread(refuse_source,runtime,scope,role,'source_changed')
            raise ValueError('frame_isolation_removed')
        until=asyncio.get_running_loop().time()+2
        while True:
            handle = await node.element_handle(timeout=500)
            child = await handle.content_frame()
            await handle.dispose()
            if child and child.url==role['resource_url'] and child.parent_frame is page.main_frame:
                break
            if role['document']!=runtime.gate.documents.get(scope['target_id']):
                raise ValueError('frame_source_changed')
            if asyncio.get_running_loop().time()>=until:
                raise ValueError('frame_document_not_ready')
            await asyncio.sleep(.02)
        facts = await child.evaluate('''()=>({credentialless:window.credentialless===true,origin:origin,
            storage:(()=>{try{localStorage.length;return false}catch(e){return e.name==='SecurityError'}})(),
            password:!!document.querySelector('input[type=password],input[autocomplete="one-time-code"]')})''')
        if facts != {'credentialless': True, 'origin': 'null', 'storage': True, 'password': False}:
            await asyncio.to_thread(refuse_source,runtime,scope,role,'identity_required' if facts.get('password') else 'isolation_unsupported')
            raise ValueError('frame_anonymous_isolation_unverified')
        session = await runtime.actor.context.new_cdp_session(child)
        try:
            target=(await session.send('Target.getTargetInfo'))['targetInfo']
            tree = await session.send('Page.getFrameTree')
            document = tree['frameTree']['frame']
            if document['id'] != role.get('frame_id') or document['url'] != role['resource_url']:
                raise ValueError('frame_document_changed')
            try:
                await session.send('Storage.getStorageKeyForFrame', {'frameId': document['id']})
            except Exception as error:
                # Chromium explicitly refuses to serialize an opaque origin.
                # Generic protocol failures never count as isolation evidence.
                if 'opaque origin and its storage key cannot be serialized' not in str(error):
                    raise ValueError('frame_storage_partition_unverified') from None
            else:
                raise ValueError('frame_storage_partition_not_opaque')
            if (await session.send('Page.getFrameTree'))['frameTree']['frame'] != document:
                raise ValueError('frame_document_changed')
        finally:
            await session.detach()
        if role['document'] != runtime.gate.documents.get(scope['target_id']):
            raise ValueError('frame_source_changed')
        role['observed_document'] = [document['id'], document['loaderId'], document['url']]
        role['frame_target']=target['targetId']
        proven_children.append(child)
        origins.append(canonical_origin(role['resource_url']))
    # The allowed origin is only an observation projection; it never broadens
    # the immutable account origin or write/request permissions.
    for frame in page.frames:
        if frame is page.main_frame or origin(frame.url) in {origin(value) for value in scope['origins']}:
            continue
        if frame not in proven_children:
            raise ValueError('frame_resource_not_approved')
    return list(dict.fromkeys(origins))


async def needs_review(controller):
    flow=(controller.lease or {}).get('auth_flow') or {}
    if not controller.lease or not controller.page or flow.get('popup_target') and not flow.get('completed'):
        return False
    facts, _ = await root_facts(controller.runtime, controller.lease, controller.page)
    with tx(controller.actor.aid) as c:
        sources = connection_sources(c, controller.runtime, controller.lease)
    approved = {(row['parent_url'], row['resource_url']) for row in sources}
    for frame in facts['frames']:
        try:
            if origin(frame['resource_url']) != origin(facts['parent_url']) and (facts['parent_url'], frame['resource_url']) not in approved:
                return True
        except ValueError:
            return True
    return False


async def guard_owner_input(controller, page, body):
    if page is not controller.page or not controller.lease.get('frame_roles'):
        return
    blocked = await page.evaluate('''point=>{const at=point.kind==='click'?document.elementFromPoint(point.x,point.y):document.activeElement;
        return at?.tagName==='IFRAME' && new URL(at.src,at.baseURI).origin!==location.origin}''',
        {'kind': body.kind, 'x': body.x, 'y': body.y})
    if blocked:
        raise ValueError('approved_frame_is_read_only')


def readonly_node(scope,node):
    return any(node.get('target_id')==role.get('frame_target') for role in scope.get('frame_roles',[]))


def finalize(aid, connection_id, account_id):
    with tx(aid) as c:
        account = one(c, 'SELECT * FROM lead.social_accounts WHERE id=%s', (account_id,))
        connection = one(c, 'SELECT * FROM lead.takeover_sessions WHERE id=%s AND completed_account_id=%s FOR UPDATE', (connection_id, account_id))
        intent = one(c, 'SELECT revision FROM lead.account_operating_intents WHERE account_id=%s', (account_id,))
        if not account or not connection or not intent:
            return
        rows = copy.deepcopy(connection['connection_data'].get('read_dependencies', []))
        for row in rows:
            if row['state'] == 'approved' and row.get('automatic_phase') == connection['challenge_version']:
                row['bound'] = {'account_id': str(account_id), 'binding_version': account['binding_version'],
                    'intent_revision': intent['revision'], 'control_version': connection['control_version'],
                    'browser_session_id': str(connection['browser_session_id']), 'browser_epoch': row['browser_epoch']}
        if rows:
            c.execute('UPDATE lead.takeover_sessions SET connection_data=connection_data || %s WHERE id=%s',
                (j({'read_dependencies': rows}), connection_id))
