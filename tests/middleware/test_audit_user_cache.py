"""审计中间件 username 反查与 token 解析逻辑测试。

DB + Redis 全部用 mock 注入，避免：
1. 测试事务隔离下新建 session 看不到未提交数据
2. Windows + asyncio 跨用例 loop 污染（"Event loop is closed"）

覆盖：
- 缓存命中：直接返回，不查 DB
- 缓存未命中：查 DB，命中后回写缓存
- DB 未命中：返回 None，不回写缓存
- token 类型校验：refresh token 不能当 access 用
- token 解析：过期/非法 token 返回 None
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt

from app.constants import REDIS_USER_NAME_PREFIX
from app.core.config import settings
from app.middleware.audit_middleware import (
    _parse_user_id_from_token,
    _resolve_username,
)


def _make_async_session_ctx_mock(scalars_first_value: str | None):
    """构造 AsyncSessionLocal 替身，scalars().first() 返回给定值。"""
    result_mock = MagicMock()
    result_mock.scalars.return_value.first.return_value = scalars_first_value
    session_mock = AsyncMock()
    session_mock.execute = AsyncMock(return_value=result_mock)

    @asynccontextmanager
    async def _ctx():
        yield session_mock

    return _ctx


def _make_redis_mock(cached_value: str | None):
    """构造 redis_client 替身，记录 set 调用。"""
    client = AsyncMock()
    client.get = AsyncMock(return_value=cached_value)
    client.set = AsyncMock()
    return client


@pytest.fixture
def fake_request_factory():
    def _make(token: str | None):
        request = MagicMock()
        request.headers = {}
        if token is not None:
            request.headers["Authorization"] = f"Bearer {token}"
        return request

    return _make


async def test_resolve_username_cache_miss_then_write():
    """缓存未命中 → 走 DB → 命中后回写缓存。"""
    user_id = 12345
    redis_mock = _make_redis_mock(cached_value=None)
    ctx_mock = _make_async_session_ctx_mock("alice")

    with (
        patch("app.middleware.audit_middleware.redis_client", redis_mock),
        patch("app.middleware.audit_middleware.AsyncSessionLocal", ctx_mock),
    ):
        name = await _resolve_username(user_id)

    assert name == "alice"
    redis_mock.get.assert_awaited_once_with(f"{REDIS_USER_NAME_PREFIX}{user_id}")
    redis_mock.set.assert_awaited_once()
    args, kwargs = redis_mock.set.call_args
    assert args[0] == f"{REDIS_USER_NAME_PREFIX}{user_id}"
    assert args[1] == "alice"
    assert kwargs.get("ex") == 300 or args[2] == 300  # 兼容位置/关键字调用


async def test_resolve_username_cache_hit_skips_db():
    """缓存命中 → 直接返回，不查 DB。"""
    user_id = 67890
    redis_mock = _make_redis_mock(cached_value="cached_name")

    @asynccontextmanager
    async def _boom():  # noqa: RUF029
        raise AssertionError("cache hit should not open DB session")
        yield  # pragma: no cover

    with (
        patch("app.middleware.audit_middleware.redis_client", redis_mock),
        patch("app.middleware.audit_middleware.AsyncSessionLocal", _boom),
    ):
        name = await _resolve_username(user_id)

    assert name == "cached_name"
    redis_mock.get.assert_awaited_once_with(f"{REDIS_USER_NAME_PREFIX}{user_id}")
    redis_mock.set.assert_not_awaited()


async def test_resolve_username_db_miss_no_cache_write():
    """DB 查不到 → 返回 None，不回写缓存。"""
    user_id = 99999
    redis_mock = _make_redis_mock(cached_value=None)
    ctx_mock = _make_async_session_ctx_mock(None)

    with (
        patch("app.middleware.audit_middleware.redis_client", redis_mock),
        patch("app.middleware.audit_middleware.AsyncSessionLocal", ctx_mock),
    ):
        name = await _resolve_username(user_id)

    assert name is None
    redis_mock.set.assert_not_awaited()


def _make_token(*, sub: str, token_type: str = "access", expired: bool = False):
    exp = datetime.now(UTC) + (
        timedelta(seconds=-10) if expired else timedelta(minutes=5)
    )
    payload = {"exp": exp, "sub": sub, "type": token_type}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def test_parse_user_id_rejects_refresh_token(fake_request_factory):
    refresh = _make_token(sub="123", token_type="refresh")
    assert _parse_user_id_from_token(fake_request_factory(refresh)) is None


def test_parse_user_id_rejects_download_token(fake_request_factory):
    download = _make_token(sub="123", token_type="ai_result_download")
    assert _parse_user_id_from_token(fake_request_factory(download)) is None


def test_parse_user_id_returns_user_id_for_access_token(fake_request_factory):
    access = _make_token(sub="456", token_type="access")
    assert _parse_user_id_from_token(fake_request_factory(access)) == 456


def test_parse_user_id_rejects_expired_token(fake_request_factory):
    expired = _make_token(sub="789", token_type="access", expired=True)
    assert _parse_user_id_from_token(fake_request_factory(expired)) is None


def test_parse_user_id_missing_auth_header(fake_request_factory):
    assert _parse_user_id_from_token(fake_request_factory(None)) is None
