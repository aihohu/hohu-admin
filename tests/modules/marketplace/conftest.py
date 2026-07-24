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
    """每个测试用独立 session，结束后强制 rollback 外层事务（绝不落库）。

    实现要点（与 ai/conftest.py 一致）：
    - 用 engine.connect() 拿独立 connection
    - 手动 conn.begin() 开 outer transaction
    - session 绑到这个 connection，session.commit() 只 commit savepoint
    - finally 块强制 outer.rollback()，无论测试通过 / 抛异常都撤销

    本 fixture 不做任何 DELETE / TRUNCATE。
    """
    _reset_redis_client()
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            async with AsyncSessionLocal(bind=conn) as session:
                yield session
        finally:
            await outer.rollback()
    # 关闭 session 后释放引擎底层连接，确保下一个测试拿到全新连接池。
    # dispose 容错：asyncpg + pytest-asyncio function-scope loop 的已知 teardown
    # race（详见 tests/modules/ai/conftest.py 同位置注释）。
    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise


@pytest.fixture
async def redis_ready():
    """独立 reset redis 客户端（用于不需要 db_session 但要访问 redis 的测试）。

    db_session fixture 已自动重置 redis，使用 db_session 的测试无需此 fixture。
    """
    _reset_redis_client()
    yield
