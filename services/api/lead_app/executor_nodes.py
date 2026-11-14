"""Managed compute identity; each actual business scope retains its own authority."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from .config import CONFIG, database_kwargs
from .db import all_rows, audit, j, one, tx
from .scope import (
    current_tenant,
    principal,
    service_scope,
    tenant_scope,
    trusted_execution,
)

MODEL, EFFORT = 'gpt-6.1-sol', 'medium'


def _id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('executor_payload_invalid') from error


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _token(kind, **values):
    raw = base64.urlsafe_b64encode(json.dumps({'kind': kind, **values}, sort_keys=True).encode()).decode().rstrip('=')
    signature = hmac.new(CONFIG['session_secret'].encode(), ('executor-v2:' + raw).encode(), hashlib.sha256).hexdigest()
    return raw + '.' + signature


def _claims(token, kind):
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        raw, signature = token.split('.')
        expected = hmac.new(CONFIG['session_secret'].encode(), ('executor-v2:' + raw).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        if value['kind'] != kind or kind == 'enrollment' and value['expires'] <= time.time():
            raise ValueError
        value['node_id'] = _id(value['node_id'])
        return value
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise ValueError('executor_node_unavailable') from error


@contextmanager
def node_transaction(node_id):
    """No default workspace or unrestricted project RLS is borrowed for auth."""
    with psycopg.connect(**database_kwargs(), row_factory=dict_row) as c:
        for key, value in {'app.tenant_id': '', 'app.agent_id': '', 'app.allowed_agent_ids': '[]',
                           'app.executor_node_id': _id(node_id), 'app.executor_grant_id': '',
                           'app.executor_studio_id': '', 'app.executor_studio_actor': ''}.items():
            c.execute('SELECT set_config(%s,%s,true)', (key, value))
        yield c


def machine_identity(node_id, tenant_id, aid=None):
    return {'tenant_id': str(tenant_id), 'issuer': 'service:executor-node', 'subject': 'executor-node:' + _id(node_id),
            'actor_type': 'service', 'role': 'service', 'node_id': _id(node_id),
            'capabilities': [], 'agent_ids': [str(aid)] if aid else []}


@contextmanager
def scope_transaction(node_id, tenant_id, *, aid=None, grant_id=None, studio=False):
    tenant_id = _id(tenant_id)
    with tenant_scope(tenant_id, machine_identity(node_id, tenant_id, aid)), service_scope(), tx(aid, tenant_id) as c:
        for key, value in {'app.executor_node_id': _id(node_id), 'app.executor_grant_id': str(grant_id or ''),
                           'app.executor_studio_id': tenant_id if studio else '', 'app.executor_studio_actor': ''}.items():
            c.execute('SELECT set_config(%s,%s,true)', (key, value))
        yield c


def policy(c):
    row = one(c, 'SELECT * FROM lead.executor_policy()')
    body = row['body'] if row and row['body'] else {'source': 'legacy', 'node_id': None}
    return {**body, 'revision': 'execution/default@' + str(row['published_version'] if row else 0)}


def _capabilities(values):
    if not isinstance(values, dict):
        raise ValueError('executor_payload_invalid')
    result = {'ready': values.get('ready') is True, 'auth_type': str(values.get('auth_type', 'signed_out'))[:80]}
    for key in ('models', 'reasoning_efforts', 'input_modalities'):
        items = values.get(key, [])
        if not isinstance(items, list) or len(items) > 100 or any(not isinstance(x, str) or len(x) > 200 for x in items):
            raise ValueError('executor_payload_invalid')
        result[key] = items
    for key in ('cli_version', 'connector_revision', 'multi_agent_version'):
        if values.get(key) is not None:
            result[key] = str(values[key])[:200]
    return result


def ready(node):
    caps = node.get('capabilities') or {}
    return bool(node['state'] == 'active' and node.get('last_seen_at')
        and (datetime.now(timezone.utc) - node['last_seen_at']).total_seconds() <= 90
        and caps.get('ready') and caps.get('auth_type') == 'chatgpt'
        and MODEL in caps.get('models', []) and EFFORT in caps.get('reasoning_efforts', []))


def _node(c, claims, token, epoch=None, *, write=False):
    node = one(c, 'SELECT * FROM lead.executor_nodes WHERE id=%s' + (' FOR UPDATE' if write else ' FOR SHARE'), (claims['node_id'],))
    if (not node or node['state'] != 'active' or node['credential_hash'] != _hash(token)
            or node['credential_generation'] != claims.get('credential_generation')):
        raise ValueError('executor_node_unavailable')
    if epoch is not None and node['epoch'] != epoch:
        raise ValueError('executor_node_epoch_changed')
    return node


def _control(c):
    from .admin_permissions import require_config
    require_config('execution/default')
    who = principal()
    if not who or who.get('tenant_id') != current_tenant():
        raise ValueError('executor_scope_forbidden')
    if trusted_execution() and who.get('actor_type') == 'service' and 'executor.manage' in who.get('capabilities', []):
        return
    if who.get('issuer') == 'local':
        from .local_executor_identity import _local_owner_mode
        if not _local_owner_mode() or who.get('subject') != 'local':
            raise ValueError('executor_scope_forbidden')
    else:
        member = one(c, 'SELECT role,status FROM lead.memberships WHERE oidc_issuer=%s AND oidc_subject=%s',
                     (who.get('issuer'), who.get('subject')))
        if not member or member['role'] != 'owner' or member['status'] != 'active':
            raise ValueError('executor_scope_forbidden')


def create_enrollment(name='server', *, allowed_tenants=None, deployment_ref=None):
    """HTTP owners enroll their workspace; an explicit deployment principal may allowlist others."""
    with tx() as c:
        _control(c)
        who = principal()
        tenants = sorted({_id(value) for value in (allowed_tenants or [current_tenant()])})
        if tenants != [current_tenant()] and not (trusted_execution() and who.get('actor_type') == 'service'
                and 'executor.enroll' in who.get('capabilities', []) and deployment_ref):
            raise ValueError('executor_scope_forbidden')
        if current_tenant() not in tenants or len(tenants) > 1000:
            raise ValueError('executor_scope_forbidden')
        node_id = str(uuid4())
        code = _token('enrollment', node_id=node_id, nonce=secrets.token_urlsafe(32), expires=int(time.time()) + 300)
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        c.execute('SELECT lead.executor_node_write(%s,%s,%s::uuid[],%s,%s,%s,%s)',
            (node_id, current_tenant(), tenants, deployment_ref or 'workspace:' + current_tenant(), name[:160], _hash(code), expires))
        audit(c, None, 'executor.node_enrollment', {'node_id': node_id, 'allowed_tenants': tenants})
    return {'node_id': node_id, 'enrollment_code': code, 'expires_at': expires.isoformat()}


def register(enrollment_code, instance_id, name, platform, capabilities):
    claims = _claims(enrollment_code, 'enrollment')
    caps = _capabilities(capabilities)
    with node_transaction(claims['node_id']) as c:
        node = one(c, 'SELECT * FROM lead.executor_nodes WHERE id=%s FOR UPDATE', (claims['node_id'],))
        if not node or node['state'] != 'pending' or node['enrollment_hash'] != _hash(enrollment_code):
            raise ValueError('executor_node_unavailable')
        token = _token('node', node_id=str(node['id']), credential_generation=node['credential_generation'], nonce=secrets.token_urlsafe(32))
        c.execute('SELECT lead.executor_node_activate(%s,%s,%s,%s,%s,%s)',
            (node['id'], _hash(enrollment_code), _hash(token), platform[:80], instance_id, j(caps)))
        tenants = node['allowed_tenants']
    for tenant in tenants:
        with scope_transaction(node['id'], tenant) as c:
            audit(c, None, 'executor.node_registered', {'node_id': str(node['id']), 'platform': platform[:80]})
    return {'node_id': str(node['id']), 'node_token': token, 'credential_generation': node['credential_generation'], 'node_epoch': node['epoch']}


def control_node(node_id, action):
    with tx() as c:
        _control(c)
        node = one(c, 'SELECT * FROM lead.executor_nodes WHERE id=%s AND control_tenant_id=lead.ctx_tenant() FOR UPDATE', (_id(node_id),))
        if not node:
            raise ValueError('executor_scope_forbidden')
        token = _token('node', node_id=str(node['id']), credential_generation=node['credential_generation'] + 1,
            nonce=secrets.token_urlsafe(32)) if action == 'rotate' else None
        c.execute('SELECT lead.executor_node_control(%s,%s,%s)', (node['id'], action, _hash(token) if token else None))
        audit(c, None, 'executor.node_' + action, {'node_id': str(node['id'])})
    return {'node_id': str(node['id']), 'node_epoch': node['epoch'] + 1,
            'credential_generation': node['credential_generation'] + 1, **({'node_token': token} if token else {'revoked': True})}


def is_managed_device(c, device_id):
    return bool(device_id and one(c, 'SELECT id FROM lead.executor_node_grants WHERE device_id=%s', (device_id,)))


def project_binding(c, aid, selected=None):
    selected = selected or policy(c)
    if selected['source'] != 'managed_node' or not selected.get('node_id'):
        raise ValueError('executor_not_ready')
    node = one(c, 'SELECT id,state FROM lead.executor_nodes WHERE id=%s', (selected['node_id'],))
    if not node or node['state'] != 'active':
        raise ValueError('executor_not_ready')
    row = one(c, 'SELECT lead.executor_grant_ensure(%s,%s,%s) AS id', (node['id'], current_tenant(), aid))
    grant = one(c, 'SELECT * FROM lead.executor_node_grants WHERE id=%s', (row['id'],))
    if not grant or grant['state'] != 'active':
        raise ValueError('executor_scope_forbidden')
    return {'device_id': grant['device_id'], 'execution_route': {'source': 'managed_node', 'grant_id': str(grant['id']),
        'grant_generation': grant['generation'], 'policy_revision': selected['revision']}}


def _public_node(node):
    if not node:
        return None
    online = bool(node['state'] == 'active' and node.get('last_seen_at')
        and (datetime.now(timezone.utc) - node['last_seen_at']).total_seconds() <= 90)
    state = ('revoked' if node['state'] == 'revoked' else 'not_configured' if node['state'] == 'pending'
        else 'offline' if not online else 'ready' if ready(node) else 'awaiting_login')
    return {'id': str(node['id']), 'name': node['name'], 'online': online, 'ready': ready(node), 'state': state,
            'auth_type': node.get('capabilities', {}).get('auth_type', 'signed_out'), 'model': MODEL, 'effort': EFFORT}


def server_status(c=None):
    with (nullcontext(c) if c is not None else tx()) as connection:
        selected = policy(connection)
        node = one(connection, 'SELECT * FROM lead.executor_nodes WHERE id=%s', (selected.get('node_id'),)) if selected.get('node_id') else None
        public = _public_node(node)
        return {'source': 'managed_node', 'configured': bool(node and node['state'] == 'active'),
            'ready': bool(public and public['ready']), 'state': public['state'] if public else 'not_configured',
            'model': MODEL, 'effort': EFFORT, 'policy_revision': selected['revision']}


def status():
    with tx() as c:
        selected = policy(c)
        node = one(c, 'SELECT * FROM lead.executor_nodes WHERE id=%s', (selected['node_id'],)) if selected.get('node_id') else one(c,
            'SELECT * FROM lead.executor_nodes WHERE lead.ctx_tenant()=ANY(allowed_tenants) ORDER BY created_at DESC LIMIT 1')
        counts = one(c, """SELECT count(*) FILTER(WHERE coalesce(p.execution_override,'inherit')='inherit') affected,
            count(*) FILTER(WHERE coalesce(p.execution_override,'inherit')<>'inherit') overrides FROM lead.agents a
            LEFT JOIN lead.local_executor_projects p ON p.agent_id=a.id AND p.tenant_id=a.tenant_id WHERE a.desired_state<>'archived'""")
    return {'policy': selected, 'node': _public_node(node), 'affected_projects': counts['affected'], 'explicit_overrides': counts['overrides']}


def poll_node(token, node_epoch, instance_id, capabilities=None):
    claims = _claims(token, 'node')
    with node_transaction(claims['node_id']) as c:
        node = _node(c, claims, token, node_epoch, write=True)
        if not one(c, 'SELECT lead.executor_node_instance(%s,%s,%s) ok', (node['id'], node_epoch, instance_id))['ok']:
            raise ValueError('executor_node_epoch_changed')
        caps = _capabilities(capabilities) if capabilities is not None else node['capabilities']
        c.execute('UPDATE lead.executor_nodes SET capabilities=%s,last_seen_at=now() WHERE id=%s', (j(caps), node['id']))
        tenants = node['allowed_tenants']
    scopes = []
    for tenant in tenants:
        with scope_transaction(node['id'], tenant) as c:
            selected = policy(c)
            if selected['source'] == 'managed_node' and selected.get('node_id') == str(node['id']):
                projects = all_rows(c, 'SELECT agent_id FROM lead.executor_catalog_projects(%s,%s)', (node['id'], tenant))
                for project in projects:
                    c.execute('SELECT lead.executor_grant_ensure(%s,%s,%s)', (node['id'], tenant, project['agent_id']))
                c.execute('SELECT lead.executor_grant_ensure(%s,%s,NULL)', (node['id'], tenant))
            grants = all_rows(c, 'SELECT * FROM lead.executor_node_grants WHERE tenant_id=%s AND node_id=%s AND state=\'active\'', (tenant, node['id']))
            for grant in grants:
                scopes.append({'scope_ref': str(grant['id']), 'scope_kind': grant['scope_kind'], 'tenant_id': str(tenant),
                    **({'agent_id': str(grant['agent_id']), 'device_id': str(grant['device_id'])} if grant['scope_kind'] == 'project'
                       else {'studio_id': str(grant['studio_id'])}), 'epoch': grant['epoch'],
                    'grant_generation': grant['generation'], 'policy_revision': grant['policy_revision']})
    return {'node_epoch': node_epoch, 'policy_revision': 'execution/default@0', 'scopes': scopes}


@contextmanager
def authorized_scope(token, scope_ref, node_epoch, grant_generation, epoch, *, instance_id=None):
    claims = _claims(token, 'node')
    with node_transaction(claims['node_id']) as c:
        node = _node(c, claims, token, node_epoch)
        grant = one(c, 'SELECT * FROM lead.executor_node_grants WHERE id=%s AND node_id=%s', (_id(scope_ref), node['id']))
        if not grant:
            raise ValueError('executor_scope_forbidden')
    with scope_transaction(node['id'], grant['tenant_id'], aid=grant['agent_id'], grant_id=grant['id'],
                           studio=grant['scope_kind'] == 'studio') as c:
        node = _node(c, claims, token, node_epoch)
        grant = one(c, 'SELECT * FROM lead.executor_grant_locked(%s)', (grant['id'],))
        if not grant or grant['node_id'] != node['id'] or grant['state'] != 'active':
            raise ValueError('executor_scope_forbidden')
        if grant['generation'] != grant_generation:
            raise ValueError('executor_scope_generation_changed')
        if grant['epoch'] != epoch:
            raise ValueError('executor_epoch_changed')
        if instance_id is not None and not one(c, 'SELECT lead.executor_grant_instance(%s,%s,%s,%s) ok',
                (grant['id'], grant_generation, epoch, instance_id))['ok']:
            raise ValueError('executor_epoch_changed')
        yield c, node, grant


def revoke_grant(scope_ref):
    with tx() as c:
        _control(c)
        grant = one(c, 'SELECT * FROM lead.executor_node_grants WHERE id=%s AND tenant_id=lead.ctx_tenant()', (_id(scope_ref),))
        if not grant:
            raise ValueError('executor_scope_forbidden')
        c.execute('SELECT lead.executor_grant_revoke(%s)', (grant['id'],))
        audit(c, None, 'executor.scope_revoked', {'scope_ref': str(grant['id'])})
    return {'revoked': True}
