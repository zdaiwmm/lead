"""Actor-owned pre-project work, never a borrowed project/device identity."""
from contextlib import contextmanager

from psycopg import sql

from . import executor_nodes as nodes
from . import local_executor as core
from .db import all_rows, audit, j, one, tx
from .local_executor_identity import capture_studio, require_studio_actor
from .scope import current_tenant

PURPOSE = 'website_brief'


def namespace(actor, value):
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError('executor_request_key_invalid')
    return 'studio:' + actor['actor_ref'] + ':' + actor['purpose'] + ':' + core._digest(value)


@contextmanager
def actor_transaction(purpose=PURPOSE):
    with tx() as c:
        actor = capture_studio(c, purpose)
        for key, value in {'app.allowed_agent_ids': '[]', 'app.agent_id': '',
                           'app.executor_node_id': '', 'app.executor_grant_id': '',
                           'app.executor_studio_id': current_tenant(), 'app.executor_studio_actor': actor['actor_ref']}.items():
            c.execute('SELECT set_config(%s,%s,true)', (key, value))
        yield c, actor


def bound(request_key, purpose=PURPOSE):
    with actor_transaction(purpose) as (c, actor):
        return bool(one(c, 'SELECT id FROM lead.executor_studio_requests WHERE scope->>\'operation_key\'=%s LIMIT 1',
                        (namespace(actor, request_key),)))


def request_by_key(request_key, purpose=PURPOSE, session_key=None):
    with actor_transaction(purpose) as (c, actor):
        row = one(c, 'SELECT * FROM lead.executor_studio_requests WHERE request_key=%s', (namespace(actor, request_key),))
        if row and session_key is not None and row['session_key'] != namespace(actor, session_key):
            raise ValueError('executor_request_conflict')
        return row


def _enqueue(c, request, kind, payload, key):
    fingerprint = core._digest(core._encoded({'kind': kind, 'payload': payload}))
    old = one(c, 'SELECT * FROM lead.executor_studio_commands WHERE request_id=%s AND command_key=%s', (request['id'], key))
    if old:
        if old['command_hash'] != fingerprint:
            raise ValueError('executor_command_conflict')
        return old
    return one(c, """INSERT INTO lead.executor_studio_commands(tenant_id,studio_id,request_id,command_key,command_hash,kind,payload)
        VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (request['tenant_id'], request['studio_id'], request['id'], key, fingerprint, kind, j(payload)))


def submit(request_key, payload, *, session_key, purpose=PURPOSE, operation_key=None):
    with actor_transaction(purpose) as (c, actor):
        key, session = namespace(actor, request_key), namespace(actor, session_key)
        if (not isinstance(payload, dict) or not isinstance(payload.get('tools', []), list)
                or any(not isinstance(tool, dict) or tool.get('name') != 'lead_public_read' for tool in payload.get('tools', []))):
            raise ValueError('executor_tool_not_declared')
        body = {**payload, 'session_key': session, 'model': core.MODEL, 'effort': core.EFFORT,
                'tool_context': {'kind': 'studio', 'purpose': purpose}}
        fingerprint = core._digest(core._encoded({'payload': body, 'actor_ref': actor['actor_ref'], 'purpose': purpose}))
        old = one(c, 'SELECT * FROM lead.executor_studio_requests WHERE request_key=%s', (key,))
        if old:
            if old['request_hash'] != fingerprint:
                raise ValueError('executor_request_conflict')
            return old
        selected = nodes.policy(c)
        if selected['source'] != 'managed_node':
            raise ValueError('executor_not_ready')
        binding = nodes.project_binding(c, None, selected)
        grant_id = binding['execution_route']['grant_id']
        scope = {'execution_route': binding['execution_route'],
                 'operation_key': namespace(actor, operation_key or request_key)}
        prior = one(c, 'SELECT grant_id,scope FROM lead.executor_studio_requests WHERE session_key=%s ORDER BY created_at,id LIMIT 1', (session,))
        if prior and (str(prior['grant_id']) != grant_id or prior['scope']['execution_route']['grant_generation'] != binding['execution_route']['grant_generation']):
            raise ValueError('executor_request_conflict')
        row = one(c, """INSERT INTO lead.executor_studio_requests(tenant_id,studio_id,grant_id,request_key,request_hash,session_key,
            actor_type,actor_ref,actor,purpose,payload,scope) VALUES(%s,%s,%s,%s,%s,%s,'user',%s,%s,%s,%s,%s) RETURNING *""",
            (current_tenant(), current_tenant(), grant_id, key, fingerprint, session, actor['actor_ref'], j(actor), purpose, j(body), j(scope)))
        _enqueue(c, row, 'run', body, 'run')
        audit(c, None, 'executor.studio_request', {'request_id': str(row['id']), 'purpose': purpose})
        return row


def request_status(request_id, purpose=PURPOSE):
    with actor_transaction(purpose) as (c, _):
        row = one(c, 'SELECT * FROM lead.executor_studio_requests WHERE id=%s', (request_id,))
        if not row:
            raise ValueError('executor_scope_forbidden')
        if row.get('result') and 'output' in row['result']:
            row['output'] = row['result']['output']
        return row


def status():
    with actor_transaction() as (c, _):
        requests = all_rows(c, 'SELECT id,state,thread_id,turn_id,usage,created_at,updated_at FROM lead.executor_studio_requests ORDER BY created_at DESC LIMIT 30')
        pending = all_rows(c, """SELECT e.event_id,e.request_id,e.method,e.params,e.created_at FROM lead.executor_studio_events e
            JOIN lead.executor_studio_requests r ON r.id=e.request_id AND r.tenant_id=e.tenant_id AND r.studio_id=e.studio_id
            WHERE r.state NOT IN ('completed','failed','interrupted','execution_unknown','canceled') AND e.method='server/request'
            AND NOT EXISTS(SELECT 1 FROM lead.executor_studio_commands c WHERE c.request_id=e.request_id AND c.kind='respond'
               AND c.payload->>'id'=e.params->>'id') ORDER BY e.created_at DESC LIMIT 20""")
        return {'requests': requests, 'pending_inputs': pending, 'source': 'managed_node', 'studio_id': current_tenant()}


def command(request_id, kind, payload, command_id):
    if kind not in {'steer', 'interrupt', 'respond'}:
        raise ValueError('executor_command_invalid')
    with actor_transaction() as (c, _):
        row = one(c, 'SELECT * FROM lead.executor_studio_requests WHERE id=%s FOR UPDATE', (request_id,))
        if not row:
            raise ValueError('executor_scope_forbidden')
        if row['state'] in core.TERMINAL:
            raise ValueError('executor_request_terminal')
        if kind == 'interrupt':
            c.execute('UPDATE lead.executor_studio_requests SET scope=scope||%s WHERE id=%s', (j({'canceled': True}), row['id']))
        elif row['scope'].get('canceled'):
            raise ValueError('executor_scope_changed')
        if kind == 'respond':
            pending = one(c, "SELECT params FROM lead.executor_studio_events WHERE request_id=%s AND method='server/request' AND params->>'id'=%s",
                          (row['id'], str(payload.get('id'))))
            if not pending:
                raise ValueError('executor_response_unavailable')
            method = pending['params'].get('method')
            if not isinstance(method, str) or method in core.MANAGED_APPROVAL_METHODS:
                raise ValueError('executor_permission_escalation_forbidden')
        item = _enqueue(c, row, kind, payload, str(command_id))
        audit(c, None, 'executor.studio_command', {'request_id': str(row['id']), 'kind': kind})
        return {'id': str(item['id']), 'request_id': str(row['id']), 'kind': kind}


def _request(c, grant, request_id):
    row = one(c, 'SELECT * FROM lead.executor_studio_requests WHERE id=%s FOR UPDATE', (request_id,))
    if (not row or row['grant_id'] != grant['id'] or row['studio_id'] != grant['studio_id']
            or row['tenant_id'] != grant['tenant_id']
            or row['scope']['execution_route'].get('grant_generation') != grant['generation']):
        raise ValueError('executor_scope_forbidden')
    return row


def poll(c, grant):
    from .admin_settings import effective
    settings, _ = effective(c, 'models')
    rows = all_rows(c, "SELECT * FROM lead.executor_studio_requests WHERE grant_id=%s AND state NOT IN ('completed','failed','interrupted','execution_unknown','canceled')", (grant['id'],))
    for row in rows:
        changed = row['scope'].get('canceled') or (row['payload'].get('web_search') == 'live' and not settings['tools']['public_web'])
        try:
            require_studio_actor(c, row['actor'])
        except ValueError:
            changed = True
        if changed:
            c.execute('UPDATE lead.executor_studio_requests SET scope=scope||%s WHERE id=%s', (j({'canceled': True}), row['id']))
            _enqueue(c, row, 'interrupt', {}, 'business-scope-fence')
    commands = all_rows(c, """SELECT c.id,c.request_id,c.kind,c.payload FROM lead.executor_studio_commands c
        JOIN lead.executor_studio_requests r ON r.id=c.request_id AND r.tenant_id=c.tenant_id AND r.studio_id=c.studio_id
        WHERE r.grant_id=%s AND c.acknowledged_at IS NULL AND r.state NOT IN
        ('completed','failed','interrupted','execution_unknown','canceled')
        AND (c.kind<>'run' OR NOT coalesce((r.scope->>'canceled')::boolean,false))
        ORDER BY c.created_at,c.id LIMIT 30""", (grant['id'],))
    return {'epoch': grant['epoch'], 'commands': [{**row, 'id': str(row['id']), 'request_id': str(row['request_id'])} for row in commands]}


def events(c, grant, entries):
    accepted = []
    for item in entries:
        request = _request(c, grant, item['request_id'])
        fingerprint = core._digest(core._encoded(item))
        old = one(c, 'SELECT * FROM lead.executor_studio_events WHERE event_id=%s', (item['id'],))
        if old:
            if old['event_hash'] != fingerprint:
                raise ValueError('executor_event_conflict')
            accepted.append(item['id'])
            continue
        method, params = item['method'], item.get('params') or {}
        change = core.event_transition(request, method, params)
        c.execute('INSERT INTO lead.executor_studio_events(tenant_id,studio_id,request_id,event_id,event_hash,method,params) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                  (grant['tenant_id'], grant['studio_id'], request['id'], item['id'], fingerprint, method, j(params)))
        fields = {key: value for key, value in change.items() if key in {'thread_id', 'turn_id', 'state', 'result', 'usage'}}
        assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in fields)
        c.execute(sql.SQL('UPDATE lead.executor_studio_requests SET {},updated_at=now() WHERE id=%s').format(assignments),
                  (*[j(value) if key in {'result', 'usage'} else value for key, value in fields.items()], request['id']))
        if 'result' in fields:
            c.execute('UPDATE lead.executor_studio_requests SET completed_at=now() WHERE id=%s', (request['id'],))
        if change.get('command_id'):
            c.execute('UPDATE lead.executor_studio_commands SET acknowledged_at=coalesce(acknowledged_at,now()) WHERE request_id=%s AND id=%s',
                      (request['id'], change['command_id']))
        elif change.get('ack_all') or change.get('ack_run'):
            c.execute("UPDATE lead.executor_studio_commands SET acknowledged_at=coalesce(acknowledged_at,now()) WHERE request_id=%s" +
                      (" AND kind='run'" if not change.get('ack_all') else ''), (request['id'],))
        accepted.append(item['id'])
    return {'epoch': grant['epoch'], 'accepted': accepted}


def tool_request(c, grant, request_id, name):
    row = _request(c, grant, request_id)
    if row['state'] in core.TERMINAL or row['scope'].get('canceled'):
        raise ValueError('executor_scope_changed')
    require_studio_actor(c, row['actor'])
    if name != 'lead_public_read' or name not in {item.get('name') for item in row['payload'].get('tools', [])}:
        raise ValueError('executor_tool_not_declared')
    return row


async def call_public_tool(request, name, arguments):
    if name != 'lead_public_read' or set(arguments) != {'url'}:
        raise ValueError('executor_tool_not_declared')
    from .admin_settings import effective
    with tx() as c:
        require_studio_actor(c, request['actor'])
        settings, _ = effective(c, 'models')
        if not settings['tools']['public_web']:
            raise ValueError('executor_public_research_disabled')
    from .fetch import fetch_product
    from .research import public_url
    observed = await fetch_product(public_url(arguments['url']), external_links=True)
    observed['producer'] = 'codex-public-read'
    return {'content': [{'type': 'text', 'text': core._encoded(observed)}], 'structuredContent': observed}
