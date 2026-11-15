"""Dedicated Node control and v2 compute transport; no project-owner impersonation."""
import asyncio
from contextlib import contextmanager
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from . import executor_nodes as nodes
from . import local_executor as executor
from .local_executor_api import Body, Command, Event
from .scope import principal, service_scope, tenant_scope

router = APIRouter()


def _bearer(request):
    value = request.headers.get('authorization', '')
    if not value.startswith('Bearer '):
        raise HTTPException(401, 'executor_node_unavailable')
    return value[7:]


class Enrollment(Body):
    name: str = Field(default='server', min_length=1, max_length=160)


class Register(Body):
    enrollment_code: str = Field(min_length=20, max_length=4096)
    instance_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=160)
    platform: str = Field(min_length=1, max_length=80)
    capabilities: dict = Field(default_factory=dict)


class NodePoll(Body):
    node_epoch: int = Field(ge=1)
    instance_id: str = Field(min_length=1, max_length=160)
    capabilities: dict | None = None


class Fence(Body):
    node_epoch: int = Field(ge=1)
    grant_generation: int = Field(ge=1)
    epoch: int = Field(ge=1)
    instance_id: str | None = Field(default=None, min_length=1, max_length=160)


class ScopePoll(Fence):
    instance_id: str = Field(min_length=1, max_length=160)


class ScopeEvents(Fence):
    events: list[Event] = Field(max_length=500)


class ScopeTools(Fence):
    request_id: UUID
    name: str = Field(min_length=1, max_length=200)
    arguments: dict = Field(default_factory=dict)


def _error(error):
    code = str(error)
    if isinstance(error, LookupError):
        return HTTPException(403, 'executor_scope_forbidden')
    if code == 'executor_node_unavailable':
        return HTTPException(401, code)
    if code in {'executor_scope_forbidden', 'executor_studio_actor_unavailable', 'executor_request_unavailable',
                'executor_tool_not_declared', 'executor_scope_changed', 'executor_permission_escalation_forbidden'}:
        return HTTPException(403, 'executor_scope_forbidden')
    if code == 'executor_not_ready':
        return HTTPException(503, code)
    if code == 'executor_capacity_unavailable':
        return HTTPException(429, code, headers={'Retry-After': '5'})
    if code in {'executor_node_epoch_changed', 'executor_epoch_changed', 'executor_scope_generation_changed', 'executor_request_conflict',
                'executor_result_conflict', 'executor_event_conflict', 'executor_command_conflict'}:
        return HTTPException(409, code)
    return HTTPException(422, 'executor_payload_invalid')


async def _run(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except (ValueError, LookupError) as error:
        raise _error(error) from None
    except psycopg.IntegrityError:
        raise HTTPException(409, 'executor_request_conflict') from None
    except psycopg.errors.InsufficientPrivilege:
        raise HTTPException(403, 'executor_scope_forbidden') from None


def _http_actor():
    if not principal():
        raise HTTPException(401, 'workspace_authentication_required')


@router.get('/api/executor/server/status')
async def server_status():
    _http_actor()
    return await _run(nodes.server_status)


@router.get('/api/executor/nodes/status')
async def nodes_status():
    _http_actor()
    return await _run(nodes.status)


@router.post('/api/executor/server/enrollment')
async def enrollment(body: Enrollment):
    _http_actor()
    return await _run(nodes.create_enrollment, body.name)


@router.post('/api/executor/server/nodes/{node_id}/{action}')
async def node_control(node_id: UUID, action: Literal['revoke', 'rotate']):
    _http_actor()
    return await _run(nodes.control_node, str(node_id), action)


@router.post('/api/executor/server/grants/{scope_ref}/revoke')
async def scope_revoke(scope_ref: UUID):
    _http_actor()
    return await _run(nodes.revoke_grant, str(scope_ref))


@router.get('/api/executor/studio/requests')
async def studio_requests():
    _http_actor()
    from .executor_studio import status
    return await _run(status)


@router.get('/api/executor/studio/requests/{request_id}')
async def studio_request(request_id: UUID):
    _http_actor()
    from .executor_studio import request_status
    return await _run(request_status, str(request_id))


@router.post('/api/executor/studio/requests/{request_id}/commands')
async def studio_command(request_id: UUID, body: Command):
    _http_actor()
    from .executor_studio import command
    return await _run(command, str(request_id), body.kind, body.payload, str(body.command_id))


@router.post('/api/agents/{aid}/executor/inherit')
async def inherit(aid: UUID):
    from .admin_permissions import require
    require('agents.control')
    return await _run(executor.set_inherit, str(aid))


@router.post('/executor/v2/nodes/register')
async def register(body: Register):
    return await _run(nodes.register, body.enrollment_code, body.instance_id, body.name, body.platform, body.capabilities)


@router.post('/executor/v2/nodes/poll')
async def node_poll(request: Request, body: NodePoll):
    return await _run(nodes.poll_node, _bearer(request), body.node_epoch, body.instance_id, body.capabilities)


def _scope_poll(token, scope_ref, body):
    from .local_executor_identity import service_device
    with nodes.authorized_scope(token, scope_ref, body.node_epoch, body.grant_generation, body.epoch,
                                instance_id=body.instance_id) as (c, node, grant):
        if grant['scope_kind'] == 'project':
            device = service_device(c, node, grant, body.epoch)
            c.execute('UPDATE lead.local_executor_devices SET last_seen_at=now(),capabilities=%s WHERE id=%s',
                      (nodes.j(node['capabilities']), device['id']))
            result = executor.poll_device(c, device, source='managed_node', grant_id=grant['id'], grant_generation=grant['generation'])
        else:
            from .executor_studio import poll
            result = poll(c, grant)
        if not nodes.ready(node):
            result['commands'] = [item for item in result['commands'] if item['kind'] != 'run']
        return result


@router.post('/executor/v2/scopes/{scope_ref}/poll')
async def scope_poll(scope_ref: UUID, request: Request, body: ScopePoll):
    return await _run(_scope_poll, _bearer(request), str(scope_ref), body)


def _scope_events(token, scope_ref, body):
    from .local_executor_identity import service_device
    with nodes.authorized_scope(token, scope_ref, body.node_epoch, body.grant_generation, body.epoch,
                                instance_id=body.instance_id) as (c, node, grant):
        entries = [event.model_dump(mode='json') for event in body.events]
        if grant['scope_kind'] == 'project':
            device = service_device(c, node, grant, body.epoch)
            return executor.device_events(c, device, body.epoch, entries, source='managed_node',
                grant_id=grant['id'], grant_generation=grant['generation'])
        from .executor_studio import events
        return events(c, grant, entries)


@router.post('/executor/v2/scopes/{scope_ref}/events')
async def scope_events(scope_ref: UUID, request: Request, body: ScopeEvents):
    return await _run(_scope_events, _bearer(request), str(scope_ref), body)


@router.post('/executor/v2/scopes/{scope_ref}/tools')
async def scope_tools(scope_ref: UUID, request: Request, body: ScopeTools):
    token = _bearer(request)
    try:
        from .local_executor_identity import service_device
        with nodes.authorized_scope(token, str(scope_ref), body.node_epoch, body.grant_generation, body.epoch,
                                    instance_id=body.instance_id) as (c, node, grant):
            if grant['scope_kind'] == 'project':
                device = service_device(c, node, grant, body.epoch)
                current = executor.tool_request(c, device, str(body.request_id), body.name, source='managed_node',
                    grant_id=grant['id'], grant_generation=grant['generation'])
            else:
                from .executor_studio import tool_request
                current = tool_request(c, grant, str(body.request_id), body.name)
                device = None
        # Keep the verified service principal for the original tool's own
        # transaction and actual account/Action/send barriers after lock release.
        with tenant_scope(grant['tenant_id'], nodes.machine_identity(node['id'], grant['tenant_id'], grant['agent_id'])), service_scope():
            if grant['scope_kind'] == 'project':
                from .local_executor_tools import call
                if (body.name == 'lead_public_read' and current.get('task_id')
                        and current['payload'].get('tool_context', {}).get('kind') == 'model'):
                    @contextmanager
                    def revalidate():
                        with nodes.authorized_scope(token, str(scope_ref), body.node_epoch,
                                body.grant_generation, body.epoch, instance_id=body.instance_id) as (c, node, grant):
                            executor.require_agent(c, str(grant['agent_id']), True)
                            current_device = service_device(c, node, grant, body.epoch)
                            checked = executor.tool_request(c, current_device, str(body.request_id), body.name,
                                source='managed_node', grant_id=grant['id'], grant_generation=grant['generation'], lock=True)
                            yield c, checked
                    return await call(device, current, body.name, body.arguments, revalidate=revalidate)
                return await call(device, current, body.name, body.arguments)
            from .executor_studio import call_public_tool
            return await call_public_tool(current, body.name, body.arguments)
    except (ValueError, LookupError) as error:
        raise _error(error) from None
    except psycopg.IntegrityError:
        raise HTTPException(409, 'executor_request_conflict') from None
    except psycopg.errors.InsufficientPrivilege:
        raise HTTPException(403, 'executor_scope_forbidden') from None
