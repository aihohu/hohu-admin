import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal


@pytest.fixture
async def db_session() -> AsyncSession:
    """每个测试用独立 session，结束自动回滚（不污染其他测试）

    用 SAVEPOINT 嵌套事务：测试代码内部可以正常 flush/commit 行为模拟，
    退出 fixture 时回滚最外层事务，所有写入都不会真正落库。
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            yield session
            # 显式回滚，防止 begin 上下文在 yield 正常结束时自动 commit
            await session.rollback()
