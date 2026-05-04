import os
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ENV: Literal["dev", "test", "prod"] = "dev"

    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 7 * 24 * 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Redis 配置
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str | None = None
    REDIS_DB: int = 0

    # 服务器访问地址 (用于拼接文件 URL)
    SERVER_URL: str = "http://127.0.0.1:8000"

    # Snowflake ID 配置
    # 每个实例应该有唯一的 worker_id（1-1023）
    # 多实例部署时必须为每个实例设置不同的值
    WORKER_ID: int = 1

    # 日期时间格式化配置
    # 用于 API 响应中的 datetime 字段格式化
    # 可通过环境变量 DATETIME_FORMAT 自定义
    DATETIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"

    # 文件上传配置
    UPLOAD_DIR: str = "uploads"
    UPLOAD_MAX_SIZE: int = 10 * 1024 * 1024  # 10MB
    UPLOAD_ALLOWED_EXTENSIONS: str = (
        ".jpg,.jpeg,.png,.gif,.webp,.pdf,.doc,.docx,.xls,.xlsx,.zip,.rar,.txt,.csv"
    )

    # 频率限制配置
    # 登录接口：每分钟最多登录尝试次数
    RATE_LIMIT_LOGIN: str = "5/minute"
    # 注册接口：每分钟最多注册尝试次数
    RATE_LIMIT_REGISTER: str = "3/minute"
    # 普通 API 接口：每分钟最多请求数
    RATE_LIMIT_API: str = "100/minute"

    # AI 配置
    AI_DEFAULT_MODEL: str = "openai:gpt-4o"
    AI_OPENAI_API_KEY: str = ""
    AI_OPENAI_BASE_URL: str = ""
    AI_ANTHROPIC_API_KEY: str = ""
    AI_MAX_TOKENS: int = 4096
    AI_TEMPERATURE: float = 0.7

    @property
    def REDIS_URL(self) -> str:
        """根据配置生成 Redis 连接字符串"""
        # 如果有密码，格式为 redis://:password@host:port/db
        # 如果没密码，格式为 redis://host:port/db
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    class Config:
        env_file = ".env"
        if os.getenv("ENV") == "test":
            env_file = ".env.test"
        elif os.getenv("ENV") == "prod":
            env_file = ".env.prod"


settings = Settings()
