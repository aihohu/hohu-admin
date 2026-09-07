from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# 1. 创建异步数据库引擎
# echo=True 会在终端打印 SQL 语句，开发环境下很有用
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_pre_ping=True,  # 自动检查连接是否存活
    pool_size=10,  # 连接池大小
    max_overflow=20,  # 超过池大小后允许的额外连接数
    pool_recycle=3600,  # 每隔一小时强制回收连接（建议小于数据库或防火墙的 idle_timeout）
    pool_timeout=30,  # 等待连接池中连接释放的最大秒数
    pool_use_lifo=True,  # 优先使用最近使用过的连接（保持连接活跃，减少被断开风险）
)

# 2. 创建异步 Session 工厂
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,  # 异步环境下通常设为 False 避免属性过期错误
)


# 3. 定义声明式基类（供 models 使用）
class Base(DeclarativeBase):
    pass


# 4. 数据库依赖注入函数 (Dependency Injection)
# 这个函数用于 FastAPI 的 Depends()
async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """
    每个请求创建一个新的异步 Session，并在请求结束时自动关闭。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            is_platform_request = (
                getattr(request.state, "platform_authorization", None) is not None
            )
            if is_platform_request:
                from app.modules.platform.audit import (  # noqa: PLC0415
                    stage_platform_success_completion,
                )

                await stage_platform_success_completion(session, request=request)
            await session.commit()
            if is_platform_request:
                request.state.platform_completion_committed = True
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
