from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture(autouse=True)
def isolated_request_throttle(monkeypatch):
    """HTTP unit tests do not share production throttle buckets or event loops.

    Dedicated middleware and Redis tests exercise throttling explicitly.
    """
    monkeypatch.setattr(
        "app.middleware.rate_limit_middleware.request_limits",
        AsyncMock(return_value={"login": 5, "register": 3, "api": 100}),
    )
    monkeypatch.setattr(
        "app.middleware.rate_limit_middleware.consume_rate_limit",
        AsyncMock(return_value=True),
    )


@pytest.fixture
async def client():
    """提供一个异步的测试客户端"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
