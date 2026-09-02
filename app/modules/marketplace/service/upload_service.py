"""[CLOUD-ONLY] 应用包文件存储 service

云市场保存 zip 到 /uploads/marketplace/，本地 HoHu 不需要存 zip（直接 install 时拉到内存解析）。
切换到 S3 或 MinIO 时只需修改此文件。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：应用包上传服务（spec 14.13）。

当前使用本地文件存储；切换存储后端时保持接口不变。
仅负责：读字节、校验 zip 魔数、计算 SHA-256、写文件并返回 URL。
不负责数据库写入（由调用方 VersionService.create 落库）。
"""

from typing import Any

from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.modules.marketplace.capability import require_marketplace_capability
from app.modules.marketplace.exceptions import (
    AppErrorCode,
    AppInvalidManifestException,
)
from app.utils.storage import compute_sha256, is_valid_zip, save_file

# 文件大小限制（spec 13.2 第 1 层审核：≤200MB）
MAX_PACKAGE_SIZE = 200 * 1024 * 1024  # 200 MB


class UploadService:
    """应用包上传服务（spec 14.13）"""

    async def save(
        self,
        *,
        file_obj: Any,
        filename: str,
        slug: str,
        version: str,
        tenant: TenantContext,
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
            BusinessException: 文件大小超过限制（FILE_TOO_LARGE）
            AppInvalidManifestException: 文件不是有效 zip（必须含至少一个 entry）
        """
        require_marketplace_capability(tenant)
        content = file_obj.read()

        # 文件大小限制（DoS 防护）
        if len(content) > MAX_PACKAGE_SIZE:
            raise BusinessException(
                code=413,
                message=f"应用包大小超过限制（{MAX_PACKAGE_SIZE // 1024 // 1024}MB）",
                error_code=AppErrorCode.FILE_TOO_LARGE,
            )

        if not is_valid_zip(content):
            raise AppInvalidManifestException(
                "上传文件不是有效的 zip 格式（必须含至少一个 entry）"
            )

        file_hash = compute_sha256(content)
        relative_path = f"marketplace/{slug}/{version}/{filename}"
        file_url = await save_file(content, relative_path=relative_path)

        return {
            "file_url": file_url,
            "file_hash": file_hash,
            "file_size": len(content),
        }


upload_service = UploadService()
