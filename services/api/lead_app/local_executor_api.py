"""Owner controls and authenticated, outbound-only local executor transport."""
import asyncio
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import local_executor as executor
from .admin_permissions import require

router = APIRouter()


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pair(Body):
    code: str = Field(min_length=20, max_length=4096)
    name: str = Field(min_length=1, max_length=160)
    platform: str = Field(min_length=1, max_length=80)
    capabilities: dict = Field(default_factory=dict)


class Poll(Body):
    epoch: int = Field(ge=1)
    instance_id: str | None = Field(default=None, min_length=1, max_length=160)
    capabilities: dict | None = None


class Event(Body):
    id: str = Field(min_length=1, max_length=200)
    request_id: UUID
    method: str = Field(min_length=1, max_length=200)
    params: dict = Field(default_factory=dict)


class Events(Body):
    epoch: int = Field(ge=1)
    events: list[Event] = Field(max_length=500)


class ToolCall(Body):
    epoch: int = Field(ge=1)
    request_id: UUID
    name: str = Field(min_length=1, max_length=200)
    arguments: dict = Field(default_factory=dict)


class Mode(Body):
    mode: Literal["codex", "legacy"]


class Command(Body):
    kind: Literal["steer", "interrupt", "respond"]
    payload: dict = Field(default_factory=dict)
    command_id: UUID = Field(default_factory=uuid4)


def _bearer(request):
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise HTTPException(401, "executor_device_unavailable")
    return value[7:]


async def _run(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        status = 401 if str(error) == "executor_device_unavailable" else 409
        raise HTTPException(status, str(error)) from error


@router.get("/api/agents/{aid}/executor")
async def executor_status(aid: UUID):
    return await _run(executor.status, str(aid))


@router.post("/api/agents/{aid}/executor/pairing")
async def executor_pairing(aid: UUID):
    require("agents.control")
    return await _run(executor.create_pairing, str(aid))


@router.post("/api/agents/{aid}/executor/mode")
async def executor_mode(aid: UUID, body: Mode):
    require("agents.control")
    return await _run(executor.set_mode, str(aid), body.mode)


@router.delete("/api/agents/{aid}/executor/devices/{device_id}")
@router.post("/api/agents/{aid}/executor/devices/{device_id}/revoke")
async def executor_revoke(aid: UUID, device_id: UUID):
    require("agents.control")
    return await _run(executor.revoke, str(aid), str(device_id))


@router.post("/api/agents/{aid}/executor/requests/{request_id}/commands")
async def executor_command(aid: UUID, request_id: UUID, body: Command):
    require("agents.chat" if body.kind == "steer" else "agents.control")
    return await _run(executor.command, str(aid), str(request_id), body.kind, body.payload, str(body.command_id))


@router.post("/executor/v1/pair")
async def pair(body: Pair):
    return await _run(executor.pair, body.code, body.name, body.platform, body.capabilities)


@router.post("/executor/v1/poll")
async def poll(request: Request, body: Poll):
    return await _run(executor.poll, _bearer(request), body.epoch,
                      instance_id=body.instance_id, capabilities=body.capabilities)


@router.post("/executor/v1/events")
async def events(request: Request, body: Events):
    return await _run(executor.events, _bearer(request), body.epoch,
                      [event.model_dump(mode="json") for event in body.events])


@router.post("/executor/v1/tools")
async def tools(request: Request, body: ToolCall):
    try:
        return await executor.call_tool(_bearer(request), body.epoch, str(body.request_id), body.name, body.arguments)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        status = 401 if str(error) == "executor_device_unavailable" else 409
        raise HTTPException(status, str(error)) from error
