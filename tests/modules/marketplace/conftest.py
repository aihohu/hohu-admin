import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal, engine


@pytest.fixture
async def db_session() -> AsyncSession:
    """每个测试用独立 session，结束自动回滚（不污染其他测试）

    用 SAVEPOINT 嵌套事务：测试代码内部可以正常 flush/commit 行为模拟，
    退出 fixture 时回滚最外层事务，所有写入都不会真正落库。

    注意：fixture 结束时 dispose engine，避免 Windows ProactorEventLoop
    重建时残留的 asyncpg 连接引发 'NoneType' send 错误（每测试一个新 loop）。
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            yield session
            # 显式回滚，防止 begin 上下文在 yield 正常结束时自动 commit
            await session.rollback()
    # 关闭 session 后释放引擎底层连接，确保下一个测试拿到全新连接池
    await engine.dispose()
