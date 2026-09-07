"""refresh_access_token 用户状态校验 + 原子 rotation 测试。

回归一：旧实现只校验 token 签名/黑名单/类型，未查 DB，导致用户被
禁用或删除后旧 refresh token 仍可在 7 天有效期内无限换 access token。

回归二：旧实现 `_is_blacklisted` 检查与 `_blacklist_token` 设置不是
原子操作，并发 refresh 同一 token 时多个请求都能通过黑名单检查，
最终签发多份新 token、且旧 token 多活一份。

修复后用 Redis SET NX 在单次操作里"检查并拉黑"，竞争失败的请求拒绝。
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt

from app.core.config import settings
from app.core.exceptions import AuthenticationException, AuthorizationException
from app.modules.auth.service import get_current_user, refresh_access_token
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User


def _make_refresh_token(*, sub: str, tenant_id: int = 0, expired: bool = False) -> str:
    exp = datetime.now(UTC) + (timedelta(seconds=-10) if expired else timedelta(days=1))
    payload = {
        "exp": exp,
        "sub": sub,
        "tid": str(tenant_id),
        "tver": "1",
        "type": "refresh",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _make_user(*, user_id: int, name: str, status: str) -> User:
    tenant = Tenant(
        tenant_id=0,
        tenant_code="default",
        tenant_name="Default Tenant",
        status="1",
        lifecycle_state="active",
        row_version=1,
    )
    user = User(
        user_id=user_id,
        tenant_id=0,
        user_name=name,
        status=status,
    )
    user.tenant = tenant
    return user


def _make_session_ctx(user: User | None):
    """构造 AsyncSessionLocal 替身，scalars().first() 返回 user。"""
    result_mock = MagicMock()
    result_mock.scalars.return_value.first.return_value = user
    session_mock = AsyncMock()
    session_mock.execute = AsyncMock(return_value=result_mock)

    @asynccontextmanager
    async def _ctx():
        yield session_mock

    return _ctx


def _make_redis_mock(*, blacklist_set_succeeds: bool = True):
    """模拟 Redis 客户端：set(nx=True) 返回是否本次抢到。"""
    client = AsyncMock()
    client.set = AsyncMock(return_value="1" if blacklist_set_succeeds else None)
    return client


async def test_refresh_success_when_user_enabled():
    """启用用户 + 抢到黑名单锁 → 成功换取新 token 对。"""
    user = _make_user(user_id=123, name="alice", status="1")
    refresh = _make_refresh_token(sub="123")

    with (
        patch("app.modules.auth.service.AsyncSessionLocal", _make_session_ctx(user)),
        patch("app.modules.auth.service.redis_client", _make_redis_mock()),
    ):
        new_access, new_refresh = await refresh_access_token(refresh)

    access_payload = jwt.decode(
        new_access, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    refresh_payload = jwt.decode(
        new_refresh, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert access_payload["type"] == "access"
    assert refresh_payload["type"] == "refresh"
    assert access_payload["sub"] == "123"
    assert access_payload["tid"] == "0"


async def test_refresh_fails_when_user_disabled():
    """禁用用户的 refresh token 必须拒绝（与 get_current_user 一样用 403）。"""
    user = _make_user(user_id=456, name="bob", status="2")
    refresh = _make_refresh_token(sub="456")

    with (
        patch("app.modules.auth.service.AsyncSessionLocal", _make_session_ctx(user)),
        patch("app.modules.auth.service.redis_client", _make_redis_mock()),
    ):
        with pytest.raises(AuthorizationException) as exc_info:
            await refresh_access_token(refresh)

    assert exc_info.value.error_code == "ACCOUNT_DISABLED"


async def test_refresh_fails_when_user_deleted():
    """用户已被删除时，refresh token 必须拒绝。"""
    refresh = _make_refresh_token(sub="999")

    with (
        patch("app.modules.auth.service.AsyncSessionLocal", _make_session_ctx(None)),
        patch("app.modules.auth.service.redis_client", _make_redis_mock()),
    ):
        with pytest.raises(AuthenticationException) as exc_info:
            await refresh_access_token(refresh)

    assert exc_info.value.error_code == "TOKEN_EXPIRED"


async def test_refresh_fails_on_concurrent_replay():
    """并发 refresh 同一 token 时，第二个请求必须被拒绝（重放保护）。

    场景：攻击者或前端 bug 同时发了两次 refresh 同一 token；或 logout 后
    旧 refresh 仍在客户端缓存被重放。Redis SET NX 保证只有一次能成功
    设置黑名单 key，另一个失败 → 拒绝。
    """
    user = _make_user(user_id=123, name="alice", status="1")
    refresh = _make_refresh_token(sub="123")

    with (
        patch("app.modules.auth.service.AsyncSessionLocal", _make_session_ctx(user)),
        patch(
            "app.modules.auth.service.redis_client",
            _make_redis_mock(blacklist_set_succeeds=False),
        ),
    ):
        with pytest.raises(AuthenticationException) as exc_info:
            await refresh_access_token(refresh)

    assert exc_info.value.error_code == "TOKEN_EXPIRED"


async def test_refresh_fails_when_token_type_wrong():
    """refresh token 类型字段错误必须拒绝。"""
    exp = datetime.now(UTC) + timedelta(minutes=5)
    payload = {
        "exp": exp,
        "sub": "123",
        "tid": "0",
        "tver": "1",
        "type": "access",
    }
    wrong = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    user = _make_user(user_id=123, name="alice", status="1")
    with (
        patch("app.modules.auth.service.AsyncSessionLocal", _make_session_ctx(user)),
        patch("app.modules.auth.service.redis_client", _make_redis_mock()),
    ):
        with pytest.raises(AuthenticationException):
            await refresh_access_token(wrong)


async def test_access_auth_rejects_download_token_type_before_database_lookup():
    """A signed download token must never authenticate an API request."""
    exp = datetime.now(UTC) + timedelta(minutes=5)
    token = jwt.encode(
        {
            "exp": exp,
            "sub": "123",
            "type": "ai_result_download",
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    user = _make_user(user_id=123, name="alice", status="1")
    result = MagicMock()
    result.scalars.return_value.first.return_value = user
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    with patch(
        "app.modules.auth.service._is_blacklisted",
        AsyncMock(return_value=False),
    ):
        with pytest.raises(AuthenticationException) as exc_info:
            await get_current_user(token=token, db=db)

    assert exc_info.value.error_code == "TOKEN_EXPIRED"
    db.execute.assert_not_awaited()
