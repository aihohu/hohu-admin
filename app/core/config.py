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
