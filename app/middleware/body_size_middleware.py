"""Enforce the deployment request ceiling even when callers bypass the proxy."""

from starlette.responses import JSONResponse

from app.core.config import settings


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = settings.UPLOAD_HARD_MAX_BYTES + 1024 * 1024
        received = 0
        exceeded = False
        rejected = False

        async def reject():
            nonlocal rejected
            if rejected:
                return
            rejected = True
            await JSONResponse(
                status_code=413,
                content={
                    "code": 413,
                    "msg": "Request body exceeds the deployment limit",
                    "data": None,
                    "errorCode": "REQUEST_BODY_TOO_LARGE",
                },
            )(scope, receive, send)

        async def bounded_send(message):
            if exceeded:
                await reject()
            else:
                await send(message)

        async def bounded_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
            if declared < 0:
                raise ValueError
        except ValueError:
            await JSONResponse(
                status_code=400,
                content={"code": 400, "msg": "Invalid Content-Length", "data": None},
            )(scope, receive, send)
            return
        try:
            if declared > limit:
                raise _BodyTooLarge
            await self.app(scope, bounded_receive, bounded_send)
        except _BodyTooLarge:
            await reject()
