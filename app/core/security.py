import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from cryptography.fernet import Fernet
from jose import jwt

from app.core.config import settings


def _get_fernet() -> Fernet:
    """从 SECRET_KEY 派生 Fernet 密钥（SHA256 → URL-safe base64）"""
    key = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_value(plaintext: str) -> str:
    """对称加密字符串，返回密文"""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """对称解密字符串，返回明文"""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证明文密码与哈希值是否匹配"""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), hashed_password.encode("utf-8")
    )


def get_password_hash(password: str) -> str:
    """生成密码哈希值"""
    pwd_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    return hashed.decode("utf-8")


def create_access_token(subject: str | Any, *, tenant_id: int) -> str:
    """生成 JWT Access Token（短期，用于 API 请求鉴权）

    ``tid`` 将 token 绑定到认证时的租户。username 等可变展示值仍不进入
    token，避免改名后污染审计日志。
    """

    expire = datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    # type 区分 access/refresh，tid 与数据库 User.tenant_id 必须二次匹配。
    to_encode: dict[str, Any] = {
        "exp": expire,
        "sub": str(subject),
        "tid": str(tenant_id),
        "type": "access",
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(subject: str | Any, *, tenant_id: int) -> str:
    """生成 JWT Refresh Token（长期，仅用于换取新的 access token）"""

    expire = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode: dict[str, Any] = {
        "exp": expire,
        "sub": str(subject),
        "tid": str(tenant_id),
        "type": "refresh",
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_platform_access_token(subject: str | Any, *, principal_version: int) -> str:
    """Issue a short-lived platform token with no tenant authority claim."""
    expire = datetime.now(UTC) + timedelta(
        minutes=settings.PLATFORM_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode: dict[str, Any] = {
        "exp": expire,
        "sub": str(subject),
        "pver": str(principal_version),
        "type": "platform_access",
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
