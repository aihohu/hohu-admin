"""utils 模块测试共用 fixture（与 system 模块同构）。"""

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.db.session import AsyncSessionLocal, engine


def _reset_redis_client() -> None:
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


@pytest.fixture
async def db_session() -> AsyncSession:
    """每个测试独立 session，结束后强制 rollback 外层事务（绝不落库）。"""
    _reset_redis_client()
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            async with AsyncSessionLocal(bind=conn) as session:
                yield session
        finally:
            await outer.rollback()
    await engine.dispose()
