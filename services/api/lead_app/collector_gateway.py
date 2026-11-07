"""Public pilot entry point. The tunnel can reach only analytics routes."""

import re

from starlette.responses import PlainTextResponse

from .api import app as api
from .promotion import CODE_PATTERN

READ = re.compile(r"/lead\.js|/r/" + CODE_PATTERN)
BROWSER = re.compile(r"/collect/[0-9a-fA-F-]{36}(?:/(?:batch|test|receipts))?")
SERVER = re.compile(r"/collect/server/(?:events|ping|conversion)")


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        return await api(scope, receive, send)
    path = scope["path"]
    method = scope.get("method")
    if (
        (method == "GET" and READ.fullmatch(path))
        or (method in {"POST", "OPTIONS"} and BROWSER.fullmatch(path))
        or (method == "POST" and SERVER.fullmatch(path))
    ):
        return await api(scope, receive, send)
    if scope["type"] == "websocket":
        return await send({"type": "websocket.close", "code": 1008})
    await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
