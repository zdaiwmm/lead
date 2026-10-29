from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class UploadLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not scope.get("path", "").startswith(("/api/", "/collect/"))
        ):
            return await self.app(scope, receive, send)
        media = scope.get("path", "").endswith("/assets")
        limit = (129 if media else 1) * 1024**2
        detail = "文件不能超过 128 MB" if media else "请求内容过大"
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length and (not length.isdigit() or int(length) > limit):
            return await JSONResponse({"detail": detail}, 413)(scope, receive, send)
        received = 0

        async def limited():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, detail)
            return message

        await self.app(scope, limited, send)
