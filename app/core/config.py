import os
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ENV: Literal["dev", "test", "prod"] = "dev"

    # 进程角色（不显式设置时按 ENV 推导）：
    # - "api": 仅承担 FastAPI，不启动调度器
    # - "scheduler": 仅承担 APScheduler（由 `python -m app.scheduler_worker` 启动）
    # - "all": 单进程同时承担 API 和调度器（开发便利）
    # 默认：ENV=dev → "all"（开发直接 fastapi dev 即可，调度器随之启动）
    #       ENV=test/prod → "api"（生产 web 进程不跑调度器，避免多 worker 重复触发）
    APP_ROLE: Literal["api", "scheduler", "all"] | None = None

    @model_validator(mode="after")
    def _resolve_app_role(self) -> "Settings":
        if self.APP_ROLE is None:
            self.APP_ROLE = "all" if self.ENV == "dev" else "api"
        return self

    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
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
    # 数据库配置
    DB_ECHO: bool = False

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

    # AI HITL（spec §8.4）
    # MVP 强制单 worker：进程内 dict[confirmation_id, asyncio.Event] 在多 worker 下静默失效。
    # v1.5+ 改 redis_pubsub 模式后可放开 WEB_CONCURRENCY
    AI_HITL_MODE: Literal["memory", "redis_pubsub"] = "memory"
    WEB_CONCURRENCY: int = 1
    # HITL 挂起 TTL（秒）：spec §8.3 默认 5min
    AI_HITL_PENDING_TTL_SEC: int = 300
    # Redis 中 args JSON 大小上限（字节）：spec §8.3 防恶意 user 撑爆 Redis
    AI_HITL_ARGS_MAX_BYTES: int = 4096

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
