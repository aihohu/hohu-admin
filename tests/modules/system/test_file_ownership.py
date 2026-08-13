"""sys_file owner/tenant persistence regression tests."""

# ruff: noqa: ASYNC240

from __future__ import annotations

import importlib.util
import io
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import UploadFile
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers

from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.modules.system.api import file as file_api
from app.modules.system.models.file import File
from app.modules.system.schemas.file import FileOut, FileQuery
from app.modules.system.service.file_service import FileService


def _upload_file(name: str, content: bytes) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=name,
        headers=Headers(
            {
                "content-type": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            }
        ),
    )


class TestFileOwnershipModel:
    def test_owner_is_nullable_only_for_legacy_compatibility(self) -> None:
        column = File.__table__.columns["owner_user_id"]

        assert isinstance(column.type, BigInteger)
        assert column.nullable is True

    def test_tenant_is_non_nullable_with_single_tenant_default(self) -> None:
        column = File.__table__.columns["tenant_id"]

        assert isinstance(column.type, BigInteger)
        assert column.nullable is False
        assert column.server_default is not None
        assert str(column.server_default.arg) == "0"


class TestFileUploadOwnership:
    async def test_upload_size_check_uses_a_bounded_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超大请求不能在校验前被完整读入进程内存。"""
        service = FileService()
        monkeypatch.setattr(settings, "UPLOAD_MAX_SIZE", 8)
        upload = MagicMock(spec=UploadFile)
        upload.read = AsyncMock(return_value=b"x" * 9)

        with pytest.raises(BusinessRuleException):
            await service._validate_size(upload)

        upload.read.assert_awaited_once_with(9)

    async def test_upload_persists_authenticated_owner_tenant_and_business_type(
        self, tmp_path: Path
    ) -> None:
        service = FileService()
        db = MagicMock(spec=AsyncSession)
        upload = _upload_file("users.xlsx", b"xlsx-content")
        service._generate_file_path = MagicMock(  # type: ignore[method-assign]
            return_value=(
                str(tmp_path / "123.xlsx"),
                "/uploads/users.xlsx",
                tmp_path,
            )
        )

        with patch("app.modules.system.service.file_service.next_id", return_value=123):
            record = await service.upload(
                db,
                upload,
                current_user_name="alice-renamed",
                owner_user_id=1001,
                tenant_id=0,
                business_type="user-import",
            )

        assert record.owner_user_id == 1001
        assert record.tenant_id == 0
        assert record.create_by == "alice-renamed"
        assert record.business_type == "user-import"
        assert Path(record.file_path).read_bytes() == b"xlsx-content"
        db.add.assert_called_once_with(record)

    async def test_user_import_upload_uses_unmounted_private_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public_root = tmp_path / "public-uploads"
        private_root = tmp_path / "private-uploads"
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(public_root))
        monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(private_root))
        service = FileService()
        db = MagicMock(spec=AsyncSession)

        with patch("app.modules.system.service.file_service.next_id", return_value=456):
            record = await service.upload(
                db,
                _upload_file("users.xlsx", b"xlsx-content"),
                current_user_name="alice",
                owner_user_id=1001,
                tenant_id=0,
                business_type="user-import",
            )

        stored_path = Path(record.file_path).resolve()
        assert stored_path.is_relative_to(private_root.resolve())
        assert not stored_path.is_relative_to(public_root.resolve())
        assert stored_path.read_bytes() == b"xlsx-content"
        assert not public_root.exists()
        assert record.file_url == ""

    async def test_server_reclassifies_chat_import_file_into_private_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public_root = tmp_path / "public-uploads"
        private_root = tmp_path / "private-uploads"
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(public_root))
        monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(private_root))
        monkeypatch.setattr(
            settings,
            "LOCAL_FILE_STORAGE_ROOT",
            str(private_root / "file_storage"),
        )
        service = FileService()
        db = MagicMock(spec=AsyncSession)

        with patch("app.modules.system.service.file_service.next_id", return_value=457):
            record = await service.upload(
                db,
                _upload_file("users.xlsx", b"xlsx-content"),
                current_user_name="alice",
                owner_user_id=1001,
                tenant_id=0,
                business_type="ai-chat",
            )

        assert record.business_type == "user-import"
        assert Path(record.file_path).resolve().is_relative_to(private_root.resolve())
        assert record.file_url == ""

    async def test_server_keeps_chat_text_attachment_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public_root = tmp_path / "public-uploads"
        private_root = tmp_path / "private-uploads"
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(public_root))
        monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(private_root))
        monkeypatch.setattr(
            settings,
            "LOCAL_FILE_STORAGE_ROOT",
            str(private_root / "file_storage"),
        )
        service = FileService()
        db = MagicMock(spec=AsyncSession)
        upload = _upload_file("notes.txt", b"private notes")
        upload.headers = Headers({"content-type": "text/plain"})

        with patch("app.modules.system.service.file_service.next_id", return_value=458):
            record = await service.upload(
                db,
                upload,
                current_user_name="alice",
                owner_user_id=1001,
                tenant_id=0,
                business_type="ai-chat",
            )

        assert record.business_type == "ai-chat-private"
        assert Path(record.file_path).resolve().is_relative_to(private_root.resolve())
        assert record.file_url == ""

    async def test_non_import_upload_remains_in_public_static_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public_root = tmp_path / "public-uploads"
        private_root = tmp_path / "private-uploads"
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(public_root))
        monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(private_root))
        service = FileService()
        db = MagicMock(spec=AsyncSession)

        with patch("app.modules.system.service.file_service.next_id", return_value=789):
            record = await service.upload(
                db,
                _upload_file("avatar.xlsx", b"xlsx-content"),
                current_user_name="alice",
                owner_user_id=1001,
                tenant_id=0,
                business_type="avatar",
            )

        stored_path = Path(record.file_path).resolve()
        assert stored_path.is_relative_to(public_root.resolve())
        assert not stored_path.is_relative_to(private_root.resolve())
        assert record.file_url.startswith("/uploads/")

    def test_private_file_empty_url_is_not_rewritten_to_server_root(self) -> None:
        schema = FileOut(
            file_id=1,
            original_name="users.xlsx",
            file_name="1",
            file_path="private_uploads/2026/08/07/1.xlsx",
            file_url="",
            file_size=1,
            file_ext=".xlsx",
            mime_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            business_type="user-import",
            create_by="alice",
            create_time=datetime(2026, 8, 7),
        )

        assert schema.model_dump(by_alias=True)["fileUrl"] == ""

    def test_file_response_does_not_expose_internal_storage_names_or_paths(
        self,
    ) -> None:
        schema = FileOut(
            file_id=1,
            original_name="users.xlsx",
            file_name="internal-snowflake-name",
            file_path="private_uploads/2026/08/07/1.xlsx",
            file_url="",
            file_size=1,
            file_ext=".xlsx",
            create_time=datetime(2026, 8, 7),
        )

        payload = schema.model_dump(by_alias=True)

        assert "fileName" not in payload
        assert "filePath" not in payload

    async def test_upload_rejects_missing_owner_before_writing_file(self) -> None:
        service = FileService()
        db = MagicMock(spec=AsyncSession)
        generate_path = MagicMock()
        service._generate_file_path = generate_path  # type: ignore[method-assign]

        with pytest.raises(AuthorizationException) as exc_info:
            await service.upload(
                db,
                _upload_file("users.xlsx", b"xlsx-content"),
                current_user_name="legacy-only",
                owner_user_id=None,  # type: ignore[arg-type]
                tenant_id=0,
                business_type="user-import",
            )

        assert exc_info.value.error_code == "FILE_OWNER_REQUIRED"
        generate_path.assert_not_called()
        db.add.assert_not_called()

    async def test_batch_upload_forwards_security_anchors_to_every_file(self) -> None:
        service = FileService()
        db = MagicMock(spec=AsyncSession)
        first = _upload_file("one.xlsx", b"one")
        second = _upload_file("two.xlsx", b"two")
        service.upload = AsyncMock(  # type: ignore[method-assign]
            side_effect=[MagicMock(spec=File), MagicMock(spec=File)]
        )

        await service.batch_upload(
            db,
            [first, second],
            current_user_name="alice",
            owner_user_id=1001,
            tenant_id=0,
            business_type="user-import",
        )

        assert service.upload.await_count == 2
        for call in service.upload.await_args_list:
            assert call.kwargs["owner_user_id"] == 1001
            assert call.kwargs["tenant_id"] == 0
            assert call.kwargs["business_type"] == "user-import"

    async def test_api_derives_tenant_from_authenticated_principal_only(self) -> None:
        db = MagicMock(spec=AsyncSession)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        current_user = MagicMock(user_id=1001, user_name="alice")
        record = MagicMock(spec=File)

        with (
            patch.object(
                file_api.file_service, "upload", new_callable=AsyncMock
            ) as save,
            patch.object(file_api, "resolve_tenant_id", return_value=0) as resolve,
        ):
            save.return_value = record
            await file_api.upload(
                file=_upload_file("users.xlsx", b"xlsx-content"),
                business_type="user-import",
                business_id=None,
                db=db,
                _current_user=current_user,
            )

        resolve.assert_called_once_with(current_user)
        assert save.await_args.kwargs["owner_user_id"] == 1001
        assert save.await_args.kwargs["tenant_id"] == 0
        assert save.await_args.kwargs["business_type"] == "user-import"

    async def test_batch_api_derives_tenant_for_all_files(self) -> None:
        db = MagicMock(spec=AsyncSession)
        db.commit = AsyncMock()
        current_user = MagicMock(user_id=1001, user_name="alice")

        with (
            patch.object(
                file_api.file_service, "batch_upload", new_callable=AsyncMock
            ) as save,
            patch.object(file_api, "resolve_tenant_id", return_value=0) as resolve,
        ):
            save.return_value = []
            await file_api.batch_upload(
                files=[_upload_file("users.xlsx", b"xlsx-content")],
                business_type="user-import",
                business_id=None,
                db=db,
                _current_user=current_user,
            )

        resolve.assert_called_once_with(current_user)
        assert save.await_args.kwargs["owner_user_id"] == 1001
        assert save.await_args.kwargs["tenant_id"] == 0
        assert save.await_args.kwargs["business_type"] == "user-import"


class TestFileDeleteOwnership:
    async def test_delete_uses_immutable_owner_id_after_username_change(self) -> None:
        service = FileService()
        db = MagicMock(spec=AsyncSession)
        db.delete = AsyncMock()
        record = MagicMock(
            spec=File,
            owner_user_id=1001,
            create_by="alice-before-rename",
            file_path="not-read-in-test",
        )
        service.get_by_id = AsyncMock(return_value=record)  # type: ignore[method-assign]
        service._delete_disk_file = MagicMock()  # type: ignore[method-assign]
        current_user = MagicMock(user_id=1001, user_name="alice-after-rename")

        await service.delete(db, 99, current_user=current_user, tenant_id=0)

        db.delete.assert_awaited_once_with(record)

    async def test_same_username_cannot_bypass_owner_id(self) -> None:
        service = FileService()
        db = MagicMock(spec=AsyncSession)
        record = MagicMock(
            spec=File,
            owner_user_id=2002,
            create_by="alice",
            file_path="not-read-in-test",
        )
        service.get_by_id = AsyncMock(return_value=record)  # type: ignore[method-assign]
        current_user = MagicMock(user_id=1001, user_name="alice")

        with pytest.raises(AuthorizationException) as exc_info:
            await service.delete(db, 99, current_user=current_user, tenant_id=0)

        assert exc_info.value.error_code == "FILE_OWNERSHIP_REQUIRED"

    def test_delete_rejects_disk_path_outside_managed_upload_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public_root = tmp_path / "public"
        private_root = tmp_path / "private"
        outside = tmp_path / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(public_root))
        monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(private_root))
        monkeypatch.setattr(
            settings,
            "LOCAL_FILE_STORAGE_ROOT",
            str(private_root / "file_storage"),
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            FileService()._delete_disk_file(str(outside))

        assert exc_info.value.error_code == "FILE_PATH_INVALID"
        assert outside.exists()


class TestFileTenantScope:
    async def test_list_always_adds_tenant_and_not_deleted_filters(self) -> None:
        service = FileService()
        db = MagicMock(spec=AsyncSession)

        with patch(
            "app.modules.system.service.file_service.paginate",
            new_callable=AsyncMock,
        ) as paginate:
            await service.get_list(db, FileQuery(), tenant_id=7)

        filters = paginate.await_args.kwargs["filters"]
        values_by_column = {
            condition.left.name: condition.right.value
            for condition in filters
            if hasattr(condition, "left") and hasattr(condition.left, "name")
        }
        assert values_by_column["tenant_id"] == 7
        assert values_by_column["del_flag"] == "0"

    async def test_detail_is_scoped_to_tenant_and_owner_for_regular_user(
        self,
    ) -> None:
        service = FileService()
        scalar_result = MagicMock()
        scalar_result.scalars.return_value.first.return_value = MagicMock(spec=File)
        db = MagicMock(spec=AsyncSession)
        db.execute = AsyncMock(return_value=scalar_result)

        await service.get_by_id(
            db,
            99,
            tenant_id=7,
            owner_user_id=1001,
            is_admin=False,
        )

        statement = db.execute.await_args.args[0]
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "sys_file.tenant_id = 7" in sql
        assert "sys_file.owner_user_id = 1001" in sql
        assert "sys_file.del_flag = '0'" in sql

    async def test_admin_detail_remains_tenant_scoped_without_owner_filter(
        self,
    ) -> None:
        service = FileService()
        scalar_result = MagicMock()
        scalar_result.scalars.return_value.first.return_value = MagicMock(spec=File)
        db = MagicMock(spec=AsyncSession)
        db.execute = AsyncMock(return_value=scalar_result)

        await service.get_by_id(db, 99, tenant_id=7, is_admin=True)

        statement = db.execute.await_args.args[0]
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "sys_file.tenant_id = 7" in sql
        assert "sys_file.owner_user_id =" not in sql

    async def test_cross_tenant_delete_never_touches_disk(self) -> None:
        service = FileService()
        scalar_result = MagicMock()
        scalar_result.scalars.return_value.first.return_value = None
        db = MagicMock(spec=AsyncSession)
        db.execute = AsyncMock(return_value=scalar_result)
        service._delete_disk_file = MagicMock()  # type: ignore[method-assign]

        with pytest.raises(NotFoundException):
            await service.delete(db, 99, is_admin=True, tenant_id=7)

        statement = db.execute.await_args.args[0]
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "sys_file.tenant_id = 7" in sql
        service._delete_disk_file.assert_not_called()

    async def test_file_apis_forward_trusted_tenant_scope(self) -> None:
        db = MagicMock(spec=AsyncSession)
        db.commit = AsyncMock()
        current_user = MagicMock(user_id=1001, user_name="alice")

        with (
            patch.object(file_api, "resolve_tenant_id", return_value=7),
            patch.object(
                file_api.file_service, "get_list", new_callable=AsyncMock
            ) as get_list,
            patch.object(
                file_api.file_service, "get_by_id", new_callable=AsyncMock
            ) as get_by_id,
            patch.object(
                file_api.file_service, "delete", new_callable=AsyncMock
            ) as delete,
            patch.object(
                file_api.file_service, "batch_delete", new_callable=AsyncMock
            ) as batch_delete,
            patch.object(file_api, "is_super_admin", return_value=False),
        ):
            await file_api.get_list(FileQuery(), db, current_user)
            await file_api.get_by_id(99, db, current_user)
            await file_api.delete(99, db, current_user)
            batch_delete.return_value = 0
            await file_api.batch_delete([99], db, current_user)

        assert get_list.await_args.kwargs["tenant_id"] == 7
        assert get_by_id.await_args.kwargs == {
            "tenant_id": 7,
            "owner_user_id": 1001,
            "is_admin": False,
        }
        assert delete.await_args.kwargs["tenant_id"] == 7
        assert batch_delete.await_args.kwargs["tenant_id"] == 7


class TestFileOwnershipMigration:
    def test_migration_adds_security_columns_and_leaves_legacy_owner_unassigned(
        self,
    ) -> None:
        migration_path = (
            Path(__file__).parents[3]
            / "alembic"
            / "versions"
            / "a6f4d2c8e1b9_add_sys_file_owner_tenant.py"
        )
        module_spec = importlib.util.spec_from_file_location(
            "task35_file_ownership_migration", migration_path
        )
        assert module_spec is not None and module_spec.loader is not None
        migration = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(migration)

        with (
            patch.object(migration.op, "add_column") as add_column,
            patch.object(migration.op, "execute") as execute,
        ):
            migration.upgrade()

        columns = {
            call.args[1].name: call.args[1] for call in add_column.call_args_list
        }
        assert columns["owner_user_id"].nullable is True
        assert isinstance(columns["owner_user_id"].type, BigInteger)
        assert columns["tenant_id"].nullable is False
        assert str(columns["tenant_id"].server_default.arg) == "0"
        execute.assert_not_called()
