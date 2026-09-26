import asyncio
import os
import warnings
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.file_storage import validate_private_storage_roots
from app.core.id_generator import next_id
from app.core.tenant import TenantContext
from app.core.tenant_scope import tenant_select
from app.modules.system.models.file import File
from app.modules.system.schemas.file import FileQuery
from app.modules.system.service.file_policy_service import (
    SCENARIO_CAPS,
    file_policy_service,
)
from app.modules.system.settings_catalog import ALLOWED_UPLOAD_EXTENSIONS
from app.utils.pagination import build_filters, paginate

PUBLIC_IMAGE_TYPES = {
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".png": ("PNG", "image/png"),
}
MAX_PUBLIC_IMAGE_PIXELS = 40_000_000
PRIVATE_BUSINESS_TYPES = frozenset({"ai-chat-private", "ai-chat-image", "user-import"})
# Browsers frequently send no usable MIME for .md (and curl users send none at
# all); the chat parser dispatches by MIME, so private text attachments get a
# canonical type stamped at upload time.
PRIVATE_TEXT_MIME_BY_EXT = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
}
OPAQUE_MIME_TYPES = frozenset({None, "", "application/octet-stream"})


class FileService:
    """文件上传业务逻辑服务"""

    async def is_public_upload(self, db: AsyncSession, file_url: str) -> bool:
        """Cross-tenant public classification; never return file metadata."""
        records = (
            await db.scalars(select(File).where(File.file_url == file_url))
        ).all()
        return bool(records) and all(
            record.del_flag == "0"
            and record.business_type not in PRIVATE_BUSINESS_TYPES | {"ai-chat"}
            for record in records
        )

    async def read_chat_image(
        self, db: AsyncSession, file_url: str, *, tenant: TenantContext
    ) -> tuple[bytes, str]:
        """Resolve an uploaded image by the current tenant and immutable owner."""
        record = await db.scalar(
            select(File).where(
                File.file_url == file_url,
                File.tenant_id == tenant.tenant_id,
                File.owner_user_id == tenant.actor_user_id,
                File.del_flag == "0",
            )
        )
        if record is None:
            raise BusinessRuleException(
                "图片已失效或无权访问，请重新上传图片",
                error_code="AI_IMAGE_NOT_AVAILABLE",
            )

        def read() -> tuple[bytes, str]:
            root = Path(
                settings.PRIVATE_UPLOAD_DIR
                if record.business_type == "ai-chat-image"
                else settings.UPLOAD_DIR
            ).resolve()
            path = Path(record.file_path).resolve()
            if (
                not path.is_relative_to(root)
                or record.file_ext not in PUBLIC_IMAGE_TYPES
                or record.business_type in PRIVATE_BUSINESS_TYPES - {"ai-chat-image"}
            ):
                raise ValueError("invalid uploaded image")
            with path.open("rb") as stream:
                content = stream.read(SCENARIO_CAPS["image"] + 1)
            if len(content) > SCENARIO_CAPS["image"]:
                raise ValueError("oversized uploaded image")
            mime = self._validate_public_image(
                content, ext=record.file_ext, declared_mime=record.mime_type
            )
            return content, mime

        try:
            return await asyncio.to_thread(read)
        except (OSError, ValueError, BusinessRuleException) as exc:
            raise BusinessRuleException(
                "图片已失效或无法读取，请重新上传图片",
                error_code="AI_IMAGE_NOT_AVAILABLE",
            ) from exc

    def _validate_extension(
        self, filename: str, allowed: frozenset[str] | None = None
    ) -> str:
        """验证文件扩展名"""
        ext = os.path.splitext(filename)[1].lower()
        allowed = allowed if allowed is not None else ALLOWED_UPLOAD_EXTENSIONS
        if ext not in allowed:
            raise BusinessRuleException(
                f"不支持的文件类型: {ext}，允许的类型: {sorted(allowed)}"
            )
        return ext

    async def _validate_size(
        self, upload_file: UploadFile, max_bytes: int | None = None
    ) -> bytes:
        """读取文件内容并验证大小"""
        max_bytes = (
            max_bytes
            if max_bytes is not None
            else min(10 * 1024 * 1024, settings.UPLOAD_HARD_MAX_BYTES)
        )
        # 只多读 1 byte 用于判定越界，避免先把攻击者控制的超大请求完整载入内存。
        content = await upload_file.read(max_bytes + 1)
        if len(content) > max_bytes:
            max_mb = max_bytes / (1024 * 1024)
            raise BusinessRuleException(f"文件大小超过限制，最大允许 {max_mb:.0f}MB")
        return content

    @staticmethod
    def _validate_public_image(
        content: bytes,
        *,
        ext: str,
        declared_mime: str | None,
    ) -> str:
        """Decode a bounded public image and return its canonical MIME type."""
        policy = PUBLIC_IMAGE_TYPES.get(ext)
        if policy is None:
            raise BusinessRuleException(
                "公开上传仅支持 JPEG 或 PNG 图片",
                error_code="PUBLIC_IMAGE_INVALID",
            )
        expected_format, expected_mime = policy
        if (declared_mime or "").strip().lower() != expected_mime:
            raise BusinessRuleException(
                "图片扩展名与声明类型不一致",
                error_code="PUBLIC_IMAGE_INVALID",
            )

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as image:
                    if image.format != expected_format:
                        raise ValueError("image format mismatch")
                    width, height = image.size
                    if (
                        width <= 0
                        or height <= 0
                        or width * height > MAX_PUBLIC_IMAGE_PIXELS
                    ):
                        raise ValueError("image dimensions exceed policy")
                    image.verify()
                # verify() checks container integrity; load() additionally forces
                # pixel decoding so truncated image data cannot be published.
                with Image.open(BytesIO(content)) as image:
                    image.load()
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            UnidentifiedImageError,
            ValueError,
        ) as exc:
            raise BusinessRuleException(
                "图片内容无效或不完整",
                error_code="PUBLIC_IMAGE_INVALID",
            ) from exc
        return expected_mime

    def _generate_file_path(
        self,
        file_name: str,
        ext: str,
        *,
        tenant_id: int,
        private: bool = False,
    ) -> tuple[str, str, Path]:
        """生成文件存储路径

        Returns:
            (relative_path, file_url, abs_dir)
        """
        if private:
            validate_private_storage_roots()
        now = datetime.now()
        tenant_dir = f"tenant-{tenant_id}"
        date_dir = f"{tenant_dir}/{now.year}/{now.month:02d}/{now.day:02d}"
        storage_root = Path(
            settings.PRIVATE_UPLOAD_DIR if private else settings.UPLOAD_DIR
        )
        abs_dir = (
            storage_root
            / tenant_dir
            / str(now.year)
            / f"{now.month:02d}"
            / f"{now.day:02d}"
        )
        abs_dir.mkdir(parents=True, exist_ok=True)
        relative_path = str(abs_dir / f"{file_name}{ext}")
        file_url = "" if private else f"/uploads/{date_dir}/{file_name}{ext}"
        return relative_path, file_url, abs_dir

    async def upload(
        self,
        db: AsyncSession,
        upload_file: UploadFile,
        current_user_name: str | None = None,
        business_type: str | None = None,
        business_id: int | None = None,
        *,
        owner_user_id: int,
        tenant: TenantContext,
    ) -> File:
        """上传单个文件"""
        if owner_user_id is None:
            raise AuthorizationException(
                "无法确定文件所有者",
                error_code="FILE_OWNER_REQUIRED",
            )
        if not upload_file.filename:
            raise BusinessRuleException("文件名不能为空")

        extension = os.path.splitext(upload_file.filename or "")[1].lower()
        scenario = (
            "image"
            if extension in PUBLIC_IMAGE_TYPES
            else "ai_file"
            if business_type == "ai-chat"
            else "import"
            if business_type == "user-import"
            else "general"
        )
        policy = await file_policy_service.resolve(db, scenario, tenant=tenant)
        ext = self._validate_extension(upload_file.filename, policy.extensions)
        content = await self._validate_size(upload_file, policy.max_bytes)
        effective_business_type = self._normalize_business_type(ext, business_type)
        private = effective_business_type in PRIVATE_BUSINESS_TYPES
        mime_type = upload_file.content_type
        if not private or effective_business_type == "ai-chat-image":
            mime_type = self._validate_public_image(
                content,
                ext=ext,
                declared_mime=upload_file.content_type,
            )
        elif (
            effective_business_type == "ai-chat-private"
            and mime_type in OPAQUE_MIME_TYPES
        ):
            mime_type = PRIVATE_TEXT_MIME_BY_EXT.get(ext, mime_type)

        file_name = str(next_id())
        relative_path, file_url, abs_dir = self._generate_file_path(
            file_name,
            ext,
            tenant_id=tenant.tenant_id,
            private=private,
        )
        if effective_business_type == "ai-chat-image":
            # Stable reference only: this object is outside the public mount.
            relative = Path(relative_path).relative_to(
                Path(settings.PRIVATE_UPLOAD_DIR)
            )
            file_url = f"/uploads/{relative.as_posix()}"

        abs_file_path = abs_dir / f"{file_name}{ext}"
        abs_file_path.write_bytes(content)

        file_record = File(
            original_name=upload_file.filename,
            file_name=file_name,
            file_path=relative_path,
            file_url=file_url,
            file_size=len(content),
            file_ext=ext,
            mime_type=mime_type,
            business_type=effective_business_type,
            business_id=business_id,
            owner_user_id=owner_user_id,
            tenant_id=tenant.tenant_id,
            create_by=current_user_name,
        )
        db.add(file_record)
        return file_record

    @staticmethod
    def _normalize_business_type(ext: str, business_type: str | None) -> str | None:
        """Keep chat spreadsheet/CSV/text uploads out of the public static root.

        The client supplies a routing hint, never the confidentiality boundary.
        Legacy ``.xls`` is quarantined privately too, even though AI parsing now
        rejects it until a maintained BIFF parser is introduced.
        """
        if business_type == "ai-chat" and ext in {".csv", ".xls", ".xlsx"}:
            return "user-import"
        if business_type == "ai-chat" and ext in {".txt", ".md", ".json"}:
            return "ai-chat-private"
        if business_type == "ai-chat" and ext in PUBLIC_IMAGE_TYPES:
            return "ai-chat-image"
        return business_type

    async def batch_upload(
        self,
        db: AsyncSession,
        upload_files: list[UploadFile],
        current_user_name: str | None = None,
        business_type: str | None = None,
        business_id: int | None = None,
        *,
        owner_user_id: int,
        tenant: TenantContext,
    ) -> list[File]:
        """批量上传文件"""
        results = []
        for f in upload_files:
            record = await self.upload(
                db,
                f,
                current_user_name=current_user_name,
                business_type=business_type,
                business_id=business_id,
                owner_user_id=owner_user_id,
                tenant=tenant,
            )
            results.append(record)
        return results

    async def get_list(
        self,
        db: AsyncSession,
        query: FileQuery,
        *,
        tenant: TenantContext,
    ):
        """获取文件分页列表"""
        field_mapping = {
            "original_name": ("original_name", "contains"),
            "business_type": ("business_type", "=="),
            "business_id": ("business_id", "=="),
            "file_ext": ("file_ext", "=="),
        }
        filters = build_filters(File, field_mapping, **query.model_dump())
        filters.extend((File.tenant_id == tenant.tenant_id, File.del_flag == "0"))

        return await paginate(
            db=db,
            model=File,
            query_params=query,
            filters=filters,
            order_by=File.create_time.desc(),
        )

    async def get_by_id(
        self,
        db: AsyncSession,
        file_id: int,
        *,
        tenant: TenantContext,
        owner_user_id: int | None = None,
        is_admin: bool = False,
    ) -> File:
        """获取单个文件详情"""
        if not is_admin and owner_user_id is None:
            raise AuthorizationException(
                "无法确定文件所有者",
                error_code="FILE_OWNER_REQUIRED",
            )
        predicates = [
            File.file_id == file_id,
            File.tenant_id == tenant.tenant_id,
            File.del_flag == "0",
        ]
        if not is_admin:
            predicates.append(File.owner_user_id == owner_user_id)
        stmt = select(File).where(*predicates)
        result = await db.execute(stmt)
        file_record = result.scalars().first()
        if not file_record:
            raise NotFoundException("文件")
        return file_record

    async def delete(
        self,
        db: AsyncSession,
        file_id: int,
        current_user: Any = None,
        is_admin: bool = False,
        *,
        tenant: TenantContext,
    ) -> None:
        """删除文件（数据库记录 + 磁盘文件）。

        - is_admin=True（超管/有 system:file:delete 权限的管理员）：直接删
        - 否则：仅当 current_user 是上传者才能删（不可变 owner_user_id）
        """
        owner_user_id = None if is_admin else getattr(current_user, "user_id", None)
        file_record = await self.get_by_id(
            db,
            file_id,
            tenant=tenant,
            owner_user_id=owner_user_id,
            is_admin=is_admin,
        )
        if not is_admin and current_user is not None:
            if file_record.owner_user_id != current_user.user_id:
                raise AuthorizationException(
                    "权限不足",
                    error_code="FILE_OWNERSHIP_REQUIRED",
                )
        self._delete_disk_file(file_record.file_path)
        await db.delete(file_record)

    async def batch_delete(
        self,
        db: AsyncSession,
        ids: list[int],
        current_user: Any = None,
        is_admin: bool = False,
        *,
        tenant: TenantContext,
    ) -> int:
        """批量删除文件；任一不可见目标使整批按 404 失败。"""
        owner_user_id = None if is_admin else getattr(current_user, "user_id", None)
        if not is_admin and owner_user_id is None:
            raise AuthorizationException(
                "无法确定文件所有者",
                error_code="FILE_OWNER_REQUIRED",
            )
        normalized = set(ids)
        predicates = [File.file_id.in_(normalized), File.del_flag == "0"]
        if not is_admin:
            predicates.append(File.owner_user_id == owner_user_id)
        records = list(
            (
                await db.execute(tenant_select(File, tenant=tenant).where(*predicates))
            ).scalars()
        )
        if {int(record.file_id) for record in records} != normalized:
            raise NotFoundException("文件")
        for record in records:
            self._delete_disk_file(record.file_path)
            await db.delete(record)
        return len(records)

    def _delete_disk_file(self, file_path: str) -> None:
        """删除磁盘文件，文件不存在时静默跳过"""
        validate_private_storage_roots()
        raw_path = Path(file_path)
        abs_path = (
            raw_path.resolve()
            if raw_path.is_absolute()
            else (Path.cwd() / raw_path).resolve()
        )
        managed_roots = (
            Path(settings.UPLOAD_DIR).resolve(),
            Path(settings.PRIVATE_UPLOAD_DIR).resolve(),
        )
        if not any(abs_path.is_relative_to(root) for root in managed_roots):
            raise BusinessRuleException(
                "文件存储路径无效",
                error_code="FILE_PATH_INVALID",
            )
        if abs_path.is_file():
            abs_path.unlink()


file_service = FileService()
