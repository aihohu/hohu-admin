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


def create_access_token(subject: str | Any, username: str = "") -> str:
    """生成 JWT Access Token"""

    expire = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    # 负载信息：sub 字段通常存放 user_id
    to_encode = {"exp": expire, "sub": str(subject)}
    if username:
        to_encode["username"] = username
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return encoded_jwt
