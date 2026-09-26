import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.requests import Request

from app.core import redis as redis_module
from app.core.security import create_access_token
from app.middleware.rate_limit_middleware import (
    RateLimitMiddleware,
    consume_rate_limit,
    request_bucket,
)


async def test_rate_limit_is_shared_and_expires(db_session):  # noqa: ARG001
    key = f"test:rate:{uuid4().hex}"
    client = redis_module.redis_client
    try:
        assert await consume_rate_limit(client, key, 2, 60)
        assert await consume_rate_limit(client, key, 2, 60)
        assert not await consume_rate_limit(client, key, 2, 60)
        assert 0 < await client.ttl(key) <= 60
    finally:
        await client.delete(key)


async def test_atomic_limit_handles_concurrent_callers(db_session):  # noqa: ARG001
    client = redis_module.redis_client
    key = f"test:rate:{uuid4().hex}"
    try:
        accepted = await asyncio.gather(
            *(consume_rate_limit(client, key, 5, 60) for _ in range(20))
        )
        assert sum(accepted) == 5
    finally:
        await client.delete(key)


def test_signed_tenant_identity_and_login_use_distinct_buckets():
    token = create_access_token("11", tenant_id=7, tenant_version=1, user_version=1)
    scope = {
        "type": "http",
        "path": "/system/user/list",
        "client": ("127.0.0.1", 1234),
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    assert request_bucket(Request(scope)) == ("api", "user:7:11")
    assert request_bucket(Request({**scope, "path": "/auth/login"})) == (
        "login",
        "ip:127.0.0.1",
    )
    assert request_bucket(
        Request({**scope, "headers": [(b"authorization", b"Bearer forged")]})
    ) == ("api", "ip:127.0.0.1")


@pytest.mark.parametrize("outage, status", [(False, 429), (True, 503)])
async def test_throttle_denies_before_business_side_effects(
    monkeypatch, outage, status
):
    counter = AsyncMock(
        return_value=False, side_effect=RedisConnectionError() if outage else None
    )
    monkeypatch.setattr(
        "app.middleware.rate_limit_middleware.consume_rate_limit", counter
    )
    downstream, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    await RateLimitMiddleware(downstream)(
        {
            "type": "http",
            "method": "GET",
            "path": "/system/user/list",
            "client": ("127.0.0.1", 123),
            "headers": [],
        },
        receive,
        send,
    )
    downstream.assert_not_awaited()
    assert send.call_args_list[0].args[0]["status"] == status
