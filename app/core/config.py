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
    # 私有上传根目录：不得通过 StaticFiles 或反向代理静态映射直接暴露
    PRIVATE_UPLOAD_DIR: str = "private_uploads"
    UPLOAD_MAX_SIZE: int = 10 * 1024 * 1024  # 10MB
    # 数据库配置
    DB_ECHO: bool = False

    UPLOAD_ALLOWED_EXTENSIONS: str = (
        ".jpg,.jpeg,.png,.gif,.webp,.pdf,.doc,.docx,.xls,.xlsx,.zip,.rar,.txt,.csv"
    )

    # 文件存储抽象（spec §3.9 v2.2 P1-4）
    # Phase 1 默认 local；Phase 3+ 切 s3 时业务代码零改动（仅切换 get_file_storage 工厂）
    FILE_STORAGE_BACKEND: Literal["local", "s3"] = "local"
    # 导入预检、失败清单和导出文件含业务数据，必须位于未静态挂载的私有根。
    LOCAL_FILE_STORAGE_ROOT: str = "private_uploads/file_storage"

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
    # spec §11.5: 整个 AI 模块全局开关（False = 不注册 AI router，安全降级）
    AI_MODULE_ENABLED: bool = True

    # AI HITL（spec §8.4 + 修订 S-6）
    # MVP 强制单 worker：进程内 dict[confirmation_id, asyncio.Event] 在多 worker 下静默失效。
    # v1.5+ 改 redis_pubsub 模式后可放开多 worker
    AI_HITL_MODE: Literal["memory", "redis_pubsub"] = "memory"
    WEB_CONCURRENCY: int = 1
    # 修订 S-6: 启动时用 Redis SADD 实测活跃 worker 数（env var WEB_CONCURRENCY
    # 不可信——uvicorn --workers 4 不经 gunicorn 时各 worker lifespan 独立检查
    # 都通过）。测试环境可关闭此检查（AI_REQUIRE_SINGLE_WORKER=False）
    AI_REQUIRE_SINGLE_WORKER: bool = True
    # HITL 挂起 TTL（秒）：spec §8.3 默认 5min
    AI_HITL_PENDING_TTL_SEC: int = 300
    # Redis 中 args JSON 大小上限（字节）：spec §8.3 防恶意 user 撑爆 Redis
    AI_HITL_ARGS_MAX_BYTES: int = 4096
    # Task 35a.0: conversation-scoped ChatCommand lease。普通流由 heartbeat 续期；
    # HITL handoff 至少延长到 confirmation TTL + grace，终态 commit 后 owner 释放。
    AI_CHAT_RUN_GUARD_TTL_SEC: int = 60
    AI_CHAT_RUN_GUARD_HEARTBEAT_SEC: int = 20
    AI_CHAT_RUN_GUARD_PENDING_GRACE_SEC: int = 60
    # spec §2.4 v1.5+: SSE 续传功能开关（默认开）
    # False 时 confirmation_required 不发 id: 字段，/ai/chat/resume 端点返回 410。
    # 关闭场景：Redis 内存紧张 / 内网部署不需要移动端续传。
    AI_SSE_RESUME_ENABLED: bool = True

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
