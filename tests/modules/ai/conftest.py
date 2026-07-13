"""ai 模块测试共用 fixture。

db_session 用 SQLAlchemy 官方推荐的「outer transaction + 强制 rollback」模式：
session 绑定到一个手动管理的外层事务；session.commit() 只提交 savepoint，不
影响外层事务；fixture 退出时强制 rollback 外层事务，所有写入撤销，绝不落库。

历史上的实现曾用 `DELETE FROM sys_user` 让 count/distinct 测试避开历史数据，
但该 DELETE 会通过测试代码中的隐式 commit 真正落库，曾造成生产 sys_user 被清空
事故。当前实现彻底移除所有 DELETE / TRUNCATE 操作 —— 测试代码可以放心 commit，
outer rollback 兜底。
"""

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.db.session import AsyncSessionLocal, engine


def _reset_redis_client() -> None:
    """每个测试新建事件循环时，重建 redis 客户端绑定到当前 loop。

    同步刷新 gateway.executor 内的 redis_client 引用（顶部 import 时绑定，
    不会跟着 redis_module.redis_client 重新解析）。
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

    # 刷新 gateway.executor 内引用
    from app.modules.ai.agents.gateway import executor as exec_mod  # noqa: PLC0415

    exec_mod.redis_client = redis_module.redis_client


@pytest.fixture
async def db_session() -> AsyncSession:
    """每个测试独立 session，结束后强制 rollback 外层事务（绝不落库）。

    实现要点：
    - 用 engine.connect() 拿独立 connection（不进连接池常规路径）
    - 手动 conn.begin() 开 outer transaction
    - session 绑到这个 connection，session.commit() 只 commit savepoint
    - finally 块强制 outer.rollback()，无论测试通过 / 抛异常都撤销
    - teardown 阶段顺手精准清理 ai_operation_log 测试残留（trace_id LIKE 'tr_test%'）；
      executor.py 内部用独立 AsyncSessionLocal() 写日志绕过了 outer transaction，
      必须靠这里兜底。不是 TRUNCATE，只删测试自己写的行。

    本 fixture 不做任何 DELETE FROM <table>（无 WHERE）/ TRUNCATE。
    """
    from sqlalchemy import text  # noqa: PLC0415

    _reset_redis_client()
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            async with AsyncSessionLocal(bind=conn) as session:
                yield session
        finally:
            await outer.rollback()
    # teardown：精准清理 ai 测试残留（executor.py 内部独立 session 写的）
    async with engine.connect() as cleanup_conn:
        await cleanup_conn.execute(
            text("DELETE FROM ai_operation_log WHERE trace_id LIKE 'tr_test%'")
        )
        await cleanup_conn.commit()
    # dispose 容错：asyncpg _terminate_graceful_close 在 loop 关闭后试图创建
    # cancel task 会抛 RuntimeError（loop closed）。这是 sqlalchemy + asyncpg
    # + pytest-asyncio 的已知 teardown race（不影响测试结果本身）。
    # pre-existing flake，try/except 兜底避免污染 pre-commit pytest hook。
    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise
