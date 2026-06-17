"""应用包上传服务（spec 14.13）。

Phase 1 用本地文件存储；Phase 2 切 S3/MinIO（接口不变，仅 save_file 内部切换）。
仅负责：读字节、校验 zip 魔数、计算 SHA-256、写文件并返回 URL。
不负责数据库写入（由调用方 VersionService.create 落库）。
"""

from typing import Any

from app.modules.marketplace.exceptions import AppInvalidManifestException
from app.utils.storage import compute_sha256, is_valid_zip, save_file


class UploadService:
    """应用包上传服务（spec 14.13）"""

    async def save(
        self,
        *,
        file_obj: Any,
        filename: str,
        slug: str,
        version: str,
    ) -> dict:
        """保存应用包。

        Args:
            file_obj: 文件二进制流（BytesIO 或类似 read() 方法的对象）
            filename: 原始文件名（用于相对路径命名）
            slug: 应用 slug
            version: 应用版本

        Returns:
            {file_url, file_hash, file_size}

        Raises:
            AppInvalidManifestException: 文件不是有效 zip
        """
        content = file_obj.read()
        if not is_valid_zip(content):
            raise AppInvalidManifestException("上传文件不是有效的 zip 格式")

        file_hash = compute_sha256(content)
        relative_path = f"marketplace/{slug}/{version}/{filename}"
        file_url = await save_file(content, relative_path=relative_path)

        return {
            "file_url": file_url,
            "file_hash": file_hash,
            "file_size": len(content),
        }


upload_service = UploadService()
