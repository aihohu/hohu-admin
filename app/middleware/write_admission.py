"""Server-owned stop gate, including GET requests which can trigger writes."""

from starlette.responses import JSONResponse

from app.core.exceptions import BusinessException
from app.core.write_admission import write_admission


class WriteAdmissionMiddleware:
    def __init__(self, app, *, gate=None):
        self.app = app
        self.gate = gate if gate is not None else write_admission

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        admission = self.gate.entry()
        try:
            await admission.__aenter__()
        except BusinessException as exc:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                await JSONResponse(
                    status_code=503,
                    content={
                        "code": 503,
                        "msg": exc.message,
                        "data": None,
                        "errorCode": exc.error_code,
                    },
                )(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            await admission.__aexit__(None, None, None)
