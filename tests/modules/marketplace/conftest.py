import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.db.session import AsyncSessionLocal, engine


def _reset_redis_client() -> None:
    """在每个测试的当前事件循环上重建 redis 连接池 + 客户端。

    pytest-asyncio 默认 per-test 新建事件循环，而 app.core.redis 中的
    redis_client 是模块级单例，其底层连接会绑定到上一个（已关闭的）loop。
    InstallService / ContributesService 会在测试中触发 redis 调用，需要
    在每个测试开始时确保 redis 客户端绑定到当前 loop。
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


@pytest.fixture
async def db_session() -> AsyncSession:
    """每个测试用独立 session，结束自动回滚（不污染其他测试）

    用 SAVEPOINT 嵌套事务：测试代码内部可以正常 flush/commit 行为模拟，
    退出 fixture 时回滚最外层事务，所有写入都不会真正落库。

    注意：fixture 结束时 dispose engine，避免 Windows ProactorEventLoop
    重建时残留的 asyncpg 连接引发 'NoneType' send 错误（每测试一个新 loop）。
    """
    _reset_redis_client()
    async with AsyncSessionLocal() as session:
        async with session.begin():
            yield session
            # 显式回滚，防止 begin 上下文在 yield 正常结束时自动 commit
            await session.rollback()
    # 关闭 session 后释放引擎底层连接，确保下一个测试拿到全新连接池
    await engine.dispose()


@pytest.fixture
async def redis_ready():
    """独立 reset redis 客户端（用于不需要 db_session 但要访问 redis 的测试）。

    db_session fixture 已自动重置 redis，使用 db_session 的测试无需此 fixture。
    """
    _reset_redis_client()
    yield
