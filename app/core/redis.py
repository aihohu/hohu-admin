import redis.asyncio as redis
from redis.asyncio import ConnectionPool

from app.core.config import settings

# 创建连接池
redis_pool = ConnectionPool.from_url(
    settings.REDIS_URL,
    encoding="utf-8",
    decode_responses=True,  # 自动将返回结果转为字符串而非 bytes
    max_connections=20,  # 最大连接数
    socket_timeout=5,  # 连接超时时间（秒）
    socket_connect_timeout=5,  # 连接建立超时时间（秒）
    retry_on_timeout=True,  # 超时时重试
)

# 创建异步 Redis 客户端
redis_client = redis.Redis(connection_pool=redis_pool)


async def get_redis():
    """供 FastAPI Depends 使用的依赖函数"""
    return redis_client


async def close_redis():
    """关闭 Redis 连接池"""
    await redis_client.close()
    await redis_pool.aclose()  # 关闭连接池
