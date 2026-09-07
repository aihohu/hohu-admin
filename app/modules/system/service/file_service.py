import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import UploadFile
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
from app.utils.pagination import build_filters, paginate


class FileService:
    """文件上传业务逻辑服务"""

    def _validate_extension(self, filename: str) -> str:
        """验证文件扩展名"""
        ext = os.path.splitext(filename)[1].lower()
        allowed = settings.UPLOAD_ALLOWED_EXTENSIONS.split(",")
        if ext not in allowed:
            raise BusinessRuleException(
                f"不支持的文件类型: {ext}，允许的类型: {settings.UPLOAD_ALLOWED_EXTENSIONS}"
            )
        return ext

    async def _validate_size(self, upload_file: UploadFile) -> bytes:
        """读取文件内容并验证大小"""
        # 只多读 1 byte 用于判定越界，避免先把攻击者控制的超大请求完整载入内存。
        content = await upload_file.read(settings.UPLOAD_MAX_SIZE + 1)
        if len(content) > settings.UPLOAD_MAX_SIZE:
            max_mb = settings.UPLOAD_MAX_SIZE / (1024 * 1024)
            raise BusinessRuleException(f"文件大小超过限制，最大允许 {max_mb:.0f}MB")
        return content

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

        ext = self._validate_extension(upload_file.filename)
        content = await self._validate_size(upload_file)
        effective_business_type = self._normalize_business_type(ext, business_type)

        file_name = str(next_id())
        relative_path, file_url, abs_dir = self._generate_file_path(
            file_name,
            ext,
            tenant_id=tenant.tenant_id,
            private=effective_business_type in {"ai-chat-private", "user-import"},
        )

        abs_file_path = abs_dir / f"{file_name}{ext}"
        abs_file_path.write_bytes(content)

        file_record = File(
            original_name=upload_file.filename,
            file_name=file_name,
            file_path=relative_path,
            file_url=file_url,
            file_size=len(content),
            file_ext=ext,
            mime_type=upload_file.content_type,
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
        if business_type == "ai-chat" and ext == ".txt":
            return "ai-chat-private"
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
