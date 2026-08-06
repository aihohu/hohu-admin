"""system 模块测试共用 fixture。

db_session 复用 marketplace 模块的 SAVEPOINT 回滚模式：测试内部可正常
flush/commit 模拟，结束时回滚最外层事务，所有写入都不真正落库。
"""

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.db.session import AsyncSessionLocal, engine


def _reset_redis_client() -> None:
    """每个测试新建事件循环时，重建 redis 客户端绑定到当前 loop。

    同步刷新多个 module-load 时绑死引用的模块（顶部 ``from app.core.redis
    import redis_client`` 不会跟着 redis_module.redis_client 重新解析）：

    - ``app.middleware.audit_middleware`` — 审计日志 user_id → username 反查
    - ``app.modules.auth.service`` — JWT 黑名单校验（``_is_blacklisted``）

    与 ``tests/modules/ai/conftest.py`` 同款，避免跨 loop ``RuntimeError:
    Event loop is closed`` 污染后续测试。
    """
    redis_module.redis_pool = aioredis.ConnectionPool.from_url(
        redis_module.settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )
    redis_module.redis_client = aioredis.Redis(connection_pool=redis_module.redis_pool)

    from app.middleware import audit_middleware as audit_mod  # noqa: PLC0415

    audit_mod.redis_client = redis_module.redis_client

    from app.modules.auth import service as auth_service  # noqa: PLC0415

    auth_service.redis_client = redis_module.redis_client


@pytest.fixture
async def db_session() -> AsyncSession:
    """每个测试独立 session，结束后强制 rollback 外层事务（绝不落库）。

    Outer-transaction 模式：session 绑定到手动管理的外层事务，session.commit()
    只提交 savepoint，fixture 退出时强制 outer.rollback() 撤销一切。本 fixture
    不做任何 DELETE / TRUNCATE。
    """
    _reset_redis_client()
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            async with AsyncSessionLocal(bind=conn) as session:
                yield session
        finally:
            await outer.rollback()
    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise
