from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.middleware.body_size_middleware import BodySizeLimitMiddleware


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"99999999")]])
async def test_large_request_is_rejected_even_when_parser_handles_exception(
    monkeypatch, headers
):
    monkeypatch.setattr(settings, "UPLOAD_HARD_MAX_BYTES", 1024)

    async def parser(_scope, receive, send):
        try:
            await receive()
        except Exception:
            # Multipart parsers may catch the receive error and emit their own 400.
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"parser failed"})

    receive = AsyncMock(
        return_value={"type": "http.request", "body": b"x" * (1024 * 1024 + 1025)}
    )
    send = AsyncMock()
    await BodySizeLimitMiddleware(parser)(
        {"type": "http", "headers": headers}, receive, send
    )
    assert send.call_args_list[0].args[0]["status"] == 413
    assert len(send.call_args_list) == 2


async def test_bad_content_length_is_a_client_error():
    app = AsyncMock()
    send = AsyncMock()
    await BodySizeLimitMiddleware(app)(
        {"type": "http", "headers": [(b"content-length", b"bad")]}, AsyncMock(), send
    )
    assert send.call_args_list[0].args[0]["status"] == 400
    app.assert_not_awaited()
