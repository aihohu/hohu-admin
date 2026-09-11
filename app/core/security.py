import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from cryptography.fernet import Fernet
from jose import jwt

from app.core.config import settings

TOKEN_ISSUER = "hohu-admin"
TENANT_ACCESS_AUDIENCE = "hohu-admin-api"
TENANT_REFRESH_AUDIENCE = "hohu-admin-refresh"
PLATFORM_ACCESS_AUDIENCE = "hohu-platform-api"


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


def _validated_version(version: int, *, name: str) -> str:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{name} must be a positive integer")
    return str(version)


def create_access_token(
    subject: str | Any,
    *,
    tenant_id: int,
    tenant_version: int,
    user_version: int,
) -> str:
    """生成 JWT Access Token（短期，用于 API 请求鉴权）

    ``tid`` 将 token 绑定到认证时的租户，``tver`` 绑定数据库安全版本。
    username 等可变展示值仍不进入 token，避免改名后污染审计日志。
    """

    expire = datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    # type 区分 access/refresh；tid/tver 必须与数据库当前租户二次匹配。
    to_encode: dict[str, Any] = {
        "exp": expire,
        "iss": TOKEN_ISSUER,
        "aud": TENANT_ACCESS_AUDIENCE,
        "sub": str(subject),
        "tid": str(tenant_id),
        "tver": _validated_version(tenant_version, name="tenant_version"),
        "uver": _validated_version(user_version, name="user_version"),
        "type": "access",
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(
    subject: str | Any,
    *,
    tenant_id: int,
    tenant_version: int,
    user_version: int,
) -> str:
    """生成 JWT Refresh Token（长期，仅用于换取新的 access token）"""

    expire = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode: dict[str, Any] = {
        "exp": expire,
        "iss": TOKEN_ISSUER,
        "aud": TENANT_REFRESH_AUDIENCE,
        "sub": str(subject),
        "tid": str(tenant_id),
        "tver": _validated_version(tenant_version, name="tenant_version"),
        "uver": _validated_version(user_version, name="user_version"),
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
        "iss": TOKEN_ISSUER,
        "aud": PLATFORM_ACCESS_AUDIENCE,
        "sub": str(subject),
        "pver": _validated_version(principal_version, name="principal_version"),
        "type": "platform_access",
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _decode_token(token: str, *, audience: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],
        audience=audience,
        issuer=TOKEN_ISSUER,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify a tenant access token against its exact issuer and audience."""
    return _decode_token(token, audience=TENANT_ACCESS_AUDIENCE)


def decode_refresh_token(token: str) -> dict[str, Any]:
    """Verify a tenant refresh token against its exact issuer and audience."""
    return _decode_token(token, audience=TENANT_REFRESH_AUDIENCE)


def decode_platform_access_token(token: str) -> dict[str, Any]:
    """Verify a platform token without accepting tenant-token audiences."""
    return _decode_token(token, audience=PLATFORM_ACCESS_AUDIENCE)


def decode_tenant_token(token: str) -> dict[str, Any]:
    """Verify either tenant token type for logout without weakening audiences."""
    token_type = jwt.get_unverified_claims(token).get("type")
    if token_type == "access":
        return decode_access_token(token)
    if token_type == "refresh":
        return decode_refresh_token(token)
    raise ValueError("unsupported tenant token type")
