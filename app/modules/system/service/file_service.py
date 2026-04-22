import os
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleException, NotFoundException
from app.core.id_generator import next_id
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
        content = await upload_file.read()
        if len(content) > settings.UPLOAD_MAX_SIZE:
            max_mb = settings.UPLOAD_MAX_SIZE / (1024 * 1024)
            raise BusinessRuleException(f"文件大小超过限制，最大允许 {max_mb:.0f}MB")
        return content

    def _generate_file_path(self, file_name: str, ext: str) -> tuple[str, str, str]:
        """生成文件存储路径

        Returns:
            (relative_path, file_url, abs_dir)
        """
        now = datetime.now()
        date_dir = f"{now.year}/{now.month:02d}/{now.day:02d}"
        relative_path = f"{settings.UPLOAD_DIR}/{date_dir}/{file_name}{ext}"
        file_url = f"/{relative_path}"
        abs_dir = (
            Path(settings.UPLOAD_DIR)
            / str(now.year)
            / f"{now.month:02d}"
            / f"{now.day:02d}"
        )
        abs_dir.mkdir(parents=True, exist_ok=True)
        return relative_path, file_url, abs_dir

    async def upload(
        self,
        db: AsyncSession,
        upload_file: UploadFile,
        current_user_name: str | None = None,
        business_type: str | None = None,
        business_id: int | None = None,
    ) -> File:
        """上传单个文件"""
        if not upload_file.filename:
            raise BusinessRuleException("文件名不能为空")

        ext = self._validate_extension(upload_file.filename)
        content = await self._validate_size(upload_file)

        file_name = str(next_id())
        relative_path, file_url, abs_dir = self._generate_file_path(file_name, ext)

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
            business_type=business_type,
            business_id=business_id,
            create_by=current_user_name,
        )
        db.add(file_record)
        return file_record

    async def batch_upload(
        self,
        db: AsyncSession,
        upload_files: list[UploadFile],
        current_user_name: str | None = None,
        business_type: str | None = None,
        business_id: int | None = None,
    ) -> list[File]:
        """批量上传文件"""
        results = []
        for f in upload_files:
            record = await self.upload(
                db, f, current_user_name, business_type, business_id
            )
            results.append(record)
        return results

    async def get_list(self, db: AsyncSession, query: FileQuery):
        """获取文件分页列表"""
        field_mapping = {
            "original_name": ("original_name", "contains"),
            "business_type": ("business_type", "=="),
            "business_id": ("business_id", "=="),
            "file_ext": ("file_ext", "=="),
        }
        filters = build_filters(File, field_mapping, **query.model_dump())

        return await paginate(
            db=db,
            model=File,
            query_params=query,
            filters=filters,
            order_by=File.create_time.desc(),
        )

    async def get_by_id(self, db: AsyncSession, file_id: int) -> File:
        """获取单个文件详情"""
        stmt = select(File).where(File.file_id == file_id)
        result = await db.execute(stmt)
        file_record = result.scalars().first()
        if not file_record:
            raise NotFoundException("文件")
        return file_record

    async def delete(self, db: AsyncSession, file_id: int) -> None:
        """删除文件（数据库记录 + 磁盘文件）"""
        file_record = await self.get_by_id(db, file_id)
        self._delete_disk_file(file_record.file_path)
        await db.delete(file_record)

    async def batch_delete(self, db: AsyncSession, ids: list[int]) -> int:
        """批量删除文件"""
        count = 0
        for file_id in ids:
            stmt = select(File).where(File.file_id == file_id)
            result = await db.execute(stmt)
            file_record = result.scalars().first()
            if file_record:
                self._delete_disk_file(file_record.file_path)
                await db.delete(file_record)
                count += 1
        return count

    def _delete_disk_file(self, file_path: str) -> None:
        """删除磁盘文件，文件不存在时静默跳过"""
        abs_path = Path(file_path)
        if abs_path.exists():
            abs_path.unlink()


file_service = FileService()
