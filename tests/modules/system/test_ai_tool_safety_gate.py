"""Built-in effect metadata and AI import file boundary tests."""

import io
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openpyxl import Workbook

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools.file_access import ProtectedFile, load_protected_file
from app.modules.ai.agents.tools.file_tools import (
    _FILE_PARSE_ACCESS_POLICY,
    file_parse,
)
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.system.ai_tools import (
    _load_file_bytes,
    user_export,
    user_import_execute,
    user_import_preview,
)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["user_name", "status"])
    worksheet.append(["alice", "1"])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _xlsx_with_extra_member(name: str, content: bytes = b"extra") -> bytes:
    source = zipfile.ZipFile(io.BytesIO(_xlsx_bytes()))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        target.writestr(name, content)
    return output.getvalue()


def _zip_bytes(name: str, content: bytes = b"not a workbook") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return output.getvalue()


def _mark_first_member_encrypted(data: bytes) -> bytes:
    """Set the encryption flag in local and central headers for a fixture."""
    mutated = bytearray(data)
    local = mutated.index(b"PK\x03\x04")
    central = mutated.index(b"PK\x01\x02")
    mutated[local + 6] |= 0x01
    mutated[central + 8] |= 0x01
    return bytes(mutated)


class TestBuiltinToolEffectMetadata:
    """Every built-in tool must have an audited replay contract."""

    def test_unknown_tool_defaults_to_write_and_non_idempotent(self) -> None:
        meta = AiToolMeta(
            name="test.unknown",
            agent="test",
            summary="unknown effect tool",
            required_perms=("test:unknown",),
            risk="low",
        )

        assert meta.readonly is False
        assert meta.idempotent is False

    def test_all_builtin_tools_have_audited_effect_metadata(self) -> None:
        # Importing these modules is the production built-in scan surface.
        from app.modules.ai.agents.tools import file_tools  # noqa: PLC0415
        from app.modules.job import ai_tools as job_tools  # noqa: PLC0415
        from app.modules.system import ai_tools as system_tools  # noqa: PLC0415

        expected = {
            # Proven pure reads: retries do not create a business side effect.
            "user.count": (True, True),
            "user.stats": (True, True),
            "user.distinct": (True, True),
            "role.count": (True, True),
            "dept.count": (True, True),
            "role.list": (True, True),
            "role.lookup": (True, True),
            "dept.list": (True, True),
            "dept.lookup": (True, True),
            "user.dept_lookup": (True, True),
            "user.role_lookup": (True, True),
            "user.list": (True, True),
            "user.lookup": (True, True),
            "file.parse": (True, True),
            # Writes without a stable replay result are conservative.
            "dept.create": (False, False),
            "dept.update": (False, False),
            "dept.move": (False, False),
            "role.create": (False, False),
            "role.update": (False, False),
            "role.update_menus": (False, False),
            "role.update_agents": (False, False),
            "user.batch_delete": (False, False),
            "user.create": (False, False),
            "user.reset_password": (False, False),
            "user.update": (False, False),
            "user.update_dept": (False, False),
            "user.update_roles": (False, False),
            "user.import_preview": (False, False),
            "user.export": (False, False),
            "job.update_cron": (False, False),
            # Execute is a write, but preview_token + CAS reuses a terminal result.
            "user.import_execute": (False, True),
        }
        actual = {
            meta.name: (meta.readonly, meta.idempotent)
            for module in (system_tools, job_tools, file_tools)
            for value in vars(module).values()
            if (meta := getattr(value, "__ai_tool_meta__", None)) is not None
        }

        assert actual == expected

    def test_preview_and_export_are_not_automatically_replayable(self) -> None:
        preview = user_import_preview.__ai_tool_meta__  # type: ignore[attr-defined]
        export = user_export.__ai_tool_meta__  # type: ignore[attr-defined]

        assert preview.risk == "low"
        assert preview.readonly is False
        assert preview.idempotent is False
        assert preview.chip_target is None
        assert preview.interaction_flow == "prepared"
        assert preview.prepared_execute_tool == "user.import_execute"
        assert "read-only" not in preview.summary.lower()
        assert "application/vnd.ms-excel" not in preview.accepts_file

        execute = user_import_execute.__ai_tool_meta__  # type: ignore[attr-defined]
        assert execute.llm_visible is False
        assert execute.hitl_always is True
        assert execute.dry_run_supported is False

        assert export.readonly is False
        assert export.idempotent is False


class TestImportPreviewArtifacts:
    async def test_same_arguments_create_distinct_preview_artifacts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        file_loader = AsyncMock(return_value=(b"csv", "users.csv", "text/csv"))
        parser = MagicMock(return_value=[SimpleNamespace(user_name="alice")])
        dry_result = SimpleNamespace(
            new_count=1,
            exists_count=0,
            conflict_count=0,
            out_of_scope_count=0,
            total=1,
        )
        batches = [
            SimpleNamespace(
                batch_id="batch-1",
                preview_token="preview-token-1",
                created_at=datetime(2026, 8, 7, 10, 0, 0),
                file_storage_key=None,
            ),
            SimpleNamespace(
                batch_id="batch-2",
                preview_token="preview-token-2",
                created_at=datetime(2026, 8, 7, 10, 0, 1),
                file_storage_key=None,
            ),
        ]
        dry_run = AsyncMock(
            side_effect=[(dry_result, batches[0]), (dry_result, batches[1])]
        )
        storage = SimpleNamespace(save=AsyncMock(side_effect=["key-1", "key-2"]))
        db = SimpleNamespace(flush=AsyncMock())
        ctx = SimpleNamespace(user=SimpleNamespace(user_id=11), db=db)
        ensure_import_permissions = AsyncMock()

        monkeypatch.setattr(
            "app.modules.system.ai_tools._load_file_bytes",
            file_loader,
        )
        monkeypatch.setattr(
            "app.modules.system.user.import_parser.parse_import_excel",
            parser,
        )
        monkeypatch.setattr(
            "app.modules.system.service.user_role_assignment_service."
            "user_role_assignment_service.ensure_import_permissions",
            ensure_import_permissions,
        )
        monkeypatch.setattr(
            "app.modules.system.user.import_service.dry_run_import_users",
            dry_run,
        )
        monkeypatch.setattr(
            "app.core.file_storage.get_file_storage",
            lambda: storage,
        )

        first = await user_import_preview(
            ctx,
            file_id="9001",
            reason="same request",
        )
        second = await user_import_preview(
            ctx,
            file_id="9001",
            reason="same request",
        )

        assert first.data["batchId"] == "batch-1"
        assert second.data["batchId"] == "batch-2"
        assert "previewToken" not in first.data
        assert first.prepared_action is not None
        assert first.prepared_action.frozen_args["preview_token"] == "preview-token-1"
        assert "preview-token-1" not in repr(first.data)
        assert "preview-token-1" not in repr(first.ui)
        assert batches[0].file_storage_key == "key-1"
        assert batches[1].file_storage_key == "key-2"
        assert dry_run.await_count == 2
        assert ensure_import_permissions.await_count == 2
        assert all(
            awaited.kwargs == {"actor_user_id": 11, "has_role_column": False}
            for awaited in ensure_import_permissions.await_args_list
        )
        assert storage.save.await_count == 2
        assert db.flush.await_count == 2

    async def test_csv_preview_artifact_executes_with_csv_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        csv_bytes = b"user_name,status\nalice,1\n"
        records = [SimpleNamespace(user_name="alice")]
        parser = MagicMock(return_value=records)
        dry_result = SimpleNamespace(
            new_count=1,
            exists_count=0,
            conflict_count=0,
            out_of_scope_count=0,
            total=1,
        )
        batch = SimpleNamespace(
            batch_id="batch-csv",
            preview_token="preview-token-csv",
            created_at=datetime(2026, 8, 7, 10, 0, 0),
            filename="users.csv",
            file_storage_key=None,
        )
        storage = SimpleNamespace(
            save=AsyncMock(return_value="import-preview/key.csv"),
            read=AsyncMock(return_value=csv_bytes),
        )
        db = SimpleNamespace(flush=AsyncMock())
        ctx = SimpleNamespace(user=SimpleNamespace(user_id=11), db=db)
        execute_result = SimpleNamespace(
            success_count=1,
            skipped_count=0,
            overwritten_count=0,
            failed_count=0,
            batch_id="batch-csv",
        )
        ensure_import_permissions = AsyncMock()

        monkeypatch.setattr(
            "app.modules.system.ai_tools._load_file_bytes",
            AsyncMock(return_value=(csv_bytes, "users.csv", "text/csv")),
        )
        monkeypatch.setattr(
            "app.modules.system.user.import_parser.parse_import_excel",
            parser,
        )
        monkeypatch.setattr(
            "app.modules.system.service.user_role_assignment_service."
            "user_role_assignment_service.ensure_import_permissions",
            ensure_import_permissions,
        )
        monkeypatch.setattr(
            "app.modules.system.user.import_service.dry_run_import_users",
            AsyncMock(return_value=(dry_result, batch)),
        )
        monkeypatch.setattr(
            "app.modules.system.user.import_service.get_batch_by_preview_token",
            AsyncMock(return_value=batch),
        )
        monkeypatch.setattr(
            "app.modules.system.user.import_service.batch_create_users_from_records",
            AsyncMock(return_value=execute_result),
        )
        monkeypatch.setattr(
            "app.core.file_storage.get_file_storage",
            lambda: storage,
        )

        await user_import_preview(
            ctx,
            file_id="9001",
            reason="csv preview",
        )
        await user_import_execute(
            ctx,
            preview_token="preview-token-csv",
            reason="csv preview",
        )

        storage.save.assert_awaited_once_with(
            csv_bytes,
            mime_type="text/csv",
            namespace="import-preview",
            suffix=".csv",
        )
        assert [call.args for call in parser.call_args_list] == [
            (csv_bytes, "text/csv"),
            (csv_bytes, "text/csv"),
        ]
        assert ensure_import_permissions.await_count == 2
        assert all(
            awaited.kwargs == {"actor_user_id": 11, "has_role_column": False}
            for awaited in ensure_import_permissions.await_args_list
        )


def _ctx_for(record: object, *, user_id: int = 11, tenant_id: int = 7) -> object:
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.first.return_value = record
    db = SimpleNamespace(execute=AsyncMock(return_value=scalar_result))
    return SimpleNamespace(
        user=SimpleNamespace(user_id=user_id),
        tenant_id=tenant_id,
        db=db,
    )


def _record(path: Path, content: bytes, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "file_id": 9001,
        "original_name": "users.xlsx",
        "file_name": "users.xlsx",
        "file_path": str(path),
        "file_size": len(content),
        "file_ext": ".xlsx",
        "mime_type": XLSX_MIME,
        "business_type": "user-import",
        "owner_user_id": 11,
        "tenant_id": 7,
        "del_flag": "0",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "private-uploads"
    root.mkdir()
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(root))
    return root


class TestLoadImportFileBoundary:
    async def test_valid_owned_tenant_scoped_xlsx_is_loaded(
        self, upload_root: Path
    ) -> None:
        content = _xlsx_bytes()
        path = upload_root / "users.xlsx"
        path.write_bytes(content)

        loaded = await _load_file_bytes(_ctx_for(_record(path, content)), "9001")

        assert loaded == (content, "users.xlsx", XLSX_MIME)

    @pytest.mark.parametrize(
        ("overrides", "ctx_kwargs"),
        [
            ({"del_flag": "1"}, {}),
            ({"owner_user_id": None}, {}),
            ({"owner_user_id": 12}, {}),
            ({"tenant_id": 8}, {}),
            ({}, {"tenant_id": 8}),
        ],
    )
    async def test_missing_deleted_or_cross_scope_is_indistinguishable(
        self,
        upload_root: Path,
        overrides: dict[str, object],
        ctx_kwargs: dict[str, int],
    ) -> None:
        # The path deliberately does not exist: authorization must reject before IO.
        content = b"PK\x03\x04safe-xlsx"
        record = _record(upload_root / "missing.xlsx", content, **overrides)

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(record, **ctx_kwargs), "9001")

        assert exc_info.value.error_code == "AI_FILE_NOT_FOUND"
        assert exc_info.value.message == "文件不存在或不可访问"

    async def test_unknown_file_is_not_found(self) -> None:
        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(None), "9001")

        assert exc_info.value.error_code == "AI_FILE_NOT_FOUND"

    async def test_wrong_business_type_is_rejected_before_io(
        self, upload_root: Path
    ) -> None:
        record = _record(
            upload_root / "missing.xlsx",
            b"PK\x03\x04safe-xlsx",
            business_type="avatar",
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(record), "9001")

        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"file_ext": ".pdf"},
            {"mime_type": "application/pdf"},
        ],
    )
    async def test_declared_extension_and_mime_are_allowlisted(
        self, upload_root: Path, overrides: dict[str, object]
    ) -> None:
        record = _record(
            upload_root / "missing.xlsx", b"PK\x03\x04safe-xlsx", **overrides
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(record), "9001")

        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    async def test_path_extension_must_match_db_extension(
        self, upload_root: Path
    ) -> None:
        content = b"PK\x03\x04safe-xlsx"
        path = upload_root / "users.csv"
        path.write_bytes(content)

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(_record(path, content)), "9001")

        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    async def test_magic_bytes_must_match_extension_and_mime(
        self, upload_root: Path
    ) -> None:
        content = b"not-an-xlsx"
        path = upload_root / "users.xlsx"
        path.write_bytes(content)

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(_record(path, content)), "9001")

        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    @pytest.mark.parametrize(
        "content",
        [
            b"PK\x03\x04damaged",
            _zip_bytes("README.txt"),
        ],
    )
    async def test_damaged_or_non_xlsx_zip_is_rejected(
        self, upload_root: Path, content: bytes
    ) -> None:
        path = upload_root / "users.xlsx"
        path.write_bytes(content)

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(_record(path, content)), "9001")

        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    @pytest.mark.parametrize(
        "content",
        [
            _xlsx_with_extra_member("../escape.txt"),
            _mark_first_member_encrypted(_xlsx_bytes()),
        ],
    )
    async def test_unsafe_zip_member_is_rejected(
        self, upload_root: Path, content: bytes
    ) -> None:
        path = upload_root / "users.xlsx"
        path.write_bytes(content)

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(_record(path, content)), "9001")

        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    @pytest.mark.parametrize(
        ("budget_name", "budget"),
        [
            ("XLSX_MAX_ZIP_ENTRIES", 1),
            ("XLSX_MAX_ENTRY_UNCOMPRESSED_BYTES", 1),
            ("XLSX_MAX_TOTAL_UNCOMPRESSED_BYTES", 1),
            ("XLSX_MAX_COMPRESSION_RATIO", 1.0),
        ],
    )
    async def test_xlsx_archive_budget_is_enforced(
        self,
        upload_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        budget_name: str,
        budget: int | float,
    ) -> None:
        content = _xlsx_bytes()
        path = upload_root / "users.xlsx"
        path.write_bytes(content)
        monkeypatch.setattr(
            f"app.modules.ai.agents.tools.file_access.{budget_name}", budget
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(_record(path, content)), "9001")

        assert exc_info.value.error_code == "AI_FILE_TOO_LARGE"

    async def test_db_declared_size_is_checked_before_io(
        self, upload_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "UPLOAD_MAX_SIZE", 8)
        record = _record(
            upload_root / "missing.xlsx",
            b"PK\x03\x04",
            file_size=9,
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(record), "9001")

        assert exc_info.value.error_code == "AI_FILE_TOO_LARGE"

    async def test_actual_size_is_bounded_even_if_db_size_is_forged(
        self, upload_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "UPLOAD_MAX_SIZE", 8)
        content = b"PK\x03\x04too-large"
        path = upload_root / "users.xlsx"
        path.write_bytes(content)
        record = _record(path, content, file_size=4)

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(record), "9001")

        assert exc_info.value.error_code == "AI_FILE_TOO_LARGE"

    async def test_path_must_resolve_under_private_upload_root(
        self, upload_root: Path
    ) -> None:
        content = b"PK\x03\x04safe-xlsx"
        outside = upload_root.parent / "outside.xlsx"
        outside.write_bytes(content)

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(_record(outside, content)), "9001")

        assert exc_info.value.error_code == "AI_FILE_PATH_INVALID"

    async def test_missing_disk_file_uses_not_found_semantics(
        self, upload_root: Path
    ) -> None:
        record = _record(
            upload_root / "missing.xlsx",
            b"PK\x03\x04safe-xlsx",
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(record), "9001")

        assert exc_info.value.error_code == "AI_FILE_NOT_FOUND"

    async def test_csv_magic_is_supported(self, upload_root: Path) -> None:
        suffix = ".csv"
        mime_type = "text/csv"
        content = b"user_name,status\nalice,1\n"
        path = upload_root / "users.csv"
        path.write_bytes(content)
        record = _record(
            path,
            content,
            original_name=f"users{suffix}",
            file_name=f"users{suffix}",
            file_ext=suffix,
            mime_type=mime_type,
        )

        loaded = await _load_file_bytes(_ctx_for(record), "9001")

        assert loaded == (content, f"users{suffix}", mime_type)

    async def test_legacy_xls_is_fail_closed_before_io(self, upload_root: Path) -> None:
        content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest"
        record = _record(
            upload_root / "missing.xls",
            content,
            original_name="users.xls",
            file_name="users.xls",
            file_ext=".xls",
            mime_type="application/vnd.ms-excel",
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await _load_file_bytes(_ctx_for(record), "9001")

        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    async def test_empty_csv_reaches_business_parser_instead_of_type_rejection(
        self, upload_root: Path
    ) -> None:
        path = upload_root / "empty.csv"
        path.write_bytes(b"")
        record = _record(
            path,
            b"",
            original_name="empty.csv",
            file_name="empty.csv",
            file_ext=".csv",
            mime_type="text/csv",
        )

        loaded = await _load_file_bytes(_ctx_for(record), "9001")

        assert loaded == (b"", "empty.csv", "text/csv")


class TestFileParseProtectedBoundary:
    async def test_file_parse_consumes_validated_bytes_without_reopening_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing_path = tmp_path / "removed-after-validation.csv"
        protected = ProtectedFile(
            record=SimpleNamespace(file_id=9001),
            path=missing_path,
            data=b"name,email\nalice,a@example.com\n",
            mime_type="text/csv",
        )
        loader = AsyncMock(return_value=protected)
        monkeypatch.setattr(
            "app.modules.ai.agents.tools.file_tools.load_protected_file",
            loader,
        )

        result = await file_parse(_ctx_for(None), file_id="9001")

        assert result.data["rows"] == 1
        assert result.data["preview"] == [{"name": "alice", "email": "a@example.com"}]
        loader.assert_awaited_once()

    @pytest.mark.parametrize(
        ("business_type", "root_name"),
        [
            ("ai-chat", "public"),
            ("ai-chat-private", "private"),
            ("user-import", "private"),
        ],
    )
    async def test_file_parse_selects_root_from_server_business_type(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        business_type: str,
        root_name: str,
    ) -> None:
        public_root = tmp_path / "public"
        private_root = tmp_path / "private"
        public_root.mkdir()
        private_root.mkdir()
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(public_root))
        monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(private_root))
        selected_root = public_root if root_name == "public" else private_root
        content = b"name\nalice\n"
        path = selected_root / "users.csv"
        path.write_bytes(content)
        record = _record(
            path,
            content,
            original_name="users.csv",
            file_name="users.csv",
            file_ext=".csv",
            mime_type="text/csv",
            business_type=business_type,
        )

        protected = await load_protected_file(
            _ctx_for(record),
            "9001",
            policy=_FILE_PARSE_ACCESS_POLICY,
        )

        assert protected.path == path.resolve()
