"""Snowflake ID 生成器配置"""

from snowflake import SnowflakeGenerator

from app.core.config import settings

# 使用配置中的 WORKER_ID，支持多实例部署
# 每个实例应该有唯一的 worker_id（1-1023）
# 在 .env 文件中设置 WORKER_ID 环境变量
generator = SnowflakeGenerator(instance=settings.WORKER_ID)


def next_id() -> int:
    """生成下一个 Snowflake ID

    Returns:
        分布式唯一 ID
    """
    return next(generator)
