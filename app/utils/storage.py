"""对象存储抽象（Phase 1 用本地文件，Phase 2 切 S3/MinIO）。

spec 14.13：应用制品包上传后落到 UPLOAD_DIR，并通过 file_url 返回前端可访问路径。
此模块只提供本地文件实现；Phase 2 切 S3/MinIO 时仅替换 save_file 内部实现，
调用方（UploadService）签名不变。
"""

import hashlib
from pathlib import Path

from app.core.config import settings


async def save_file(content: bytes, *, relative_path: str) -> str:
    """保存文件到本地存储，返回相对 URL（前端用 SERVER_URL 拼接）。

    Args:
        content: 文件二进制内容
        relative_path: 相对 UPLOAD_DIR 的路径（如 marketplace/foo/1.0.0/foo-1.0.0.zip）

    Returns:
        可访问的 URL 路径（形如 /uploads/marketplace/foo/1.0.0/foo-1.0.0.zip），
        前端按需拼接 SERVER_URL 拿到完整 URL。
    """
    full_path = Path(settings.UPLOAD_DIR) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(content)
    return f"/uploads/{relative_path}"


def compute_sha256(content: bytes) -> str:
    """计算 SHA-256（spec 14.13 完整性校验）。

    Args:
        content: 待计算的二进制内容

    Returns:
        64 字符小写十六进制 SHA-256 摘要
    """
    return hashlib.sha256(content).hexdigest()


def is_valid_zip(content: bytes) -> bool:
    """简单校验 zip 魔数（PK\\x03\\x04 普通文件 / PK\\x05\\x06 空归档）。

    Args:
        content: 待校验的二进制内容

    Returns:
        True 表示前 4 字节匹配 zip 局部头或结尾归档标记
    """
    return content[:4] == b"PK\x03\x04" or content[:4] == b"PK\x05\x06"
