"""用户导入导出 Pydantic schemas 测试（Task 1）。

覆盖：
- UserImportRecord：必填 / Literal / default 值
- FailedRow：必填
- ImportDryRunResult：default truncated=False + count property
- ImportResult：default idempotent_replay=False
- UserExportFilter：可选字段
- UserImportBatchResponse / UserExportTaskResponse：operator_id 字符串序列化（防 JS BigInt 精度丢失）
- camelCase 序列化（alias_generator=to_camel）
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.modules.system.user.schemas import (
    FailedRow,
    ImportDryRunResult,
    ImportResult,
    UserExportFilter,
    UserExportTaskResponse,
    UserImportBatchResponse,
    UserImportRecord,
)


def _sample_record_kwargs(**overrides):
    base = {
        "row_num": 2,
        "user_name": "alice",
        "dept_input": "前端部",
    }
    base.update(overrides)
    return base


class TestUserImportRecord:
    def test_required_fields_minimal(self):
        rec = UserImportRecord(**_sample_record_kwargs())
        assert rec.user_name == "alice"
        assert rec.dept_input == "前端部"
        assert rec.user_gender == "0"
        assert rec.status == "1"
        assert rec.employee_no is None

    def test_missing_user_name_rejected(self):
        with pytest.raises(ValidationError):
            UserImportRecord(row_num=2, dept_input="x")

    def test_missing_dept_input_rejected(self):
        with pytest.raises(ValidationError):
            UserImportRecord(row_num=2, user_name="alice")

    def test_invalid_user_gender_rejected(self):
        with pytest.raises(ValidationError):
            UserImportRecord(**_sample_record_kwargs(user_gender="3"))

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            UserImportRecord(**_sample_record_kwargs(status="2"))

    def test_user_name_too_short(self):
        with pytest.raises(ValidationError):
            UserImportRecord(**_sample_record_kwargs(user_name="a"))

    def test_camel_case_alias(self):
        rec = UserImportRecord(**_sample_record_kwargs())
        serialized = rec.model_dump(by_alias=True)
        assert "rowNum" in serialized
        assert "userName" in serialized
        assert "deptInput" in serialized
        assert "roleInput" in serialized
        assert "userGender" in serialized


class TestFailedRow:
    def test_required_fields(self):
        row = FailedRow(
            row_num=5,
            field="user_name",
            value="",
            reason="必填字段缺失",
            error_code="AI_IMPORT_USERNAME_INVALID",
        )
        assert row.error_code == "AI_IMPORT_USERNAME_INVALID"

    def test_missing_field_rejected(self):
        with pytest.raises(ValidationError):
            FailedRow(row_num=5, value="", reason="x", error_code="X")  # type: ignore[call-arg]


class TestImportDryRunResult:
    def test_defaults_truncated_false(self):
        result = ImportDryRunResult(total=10)
        assert result.new_records_truncated is False
        assert result.exists_records_truncated is False
        assert result.conflict_records_truncated is False
        assert result.out_of_scope_records_truncated is False

    def test_default_records_empty_list(self):
        result = ImportDryRunResult(total=0)
        assert result.new_records == []
        assert result.exists_records == []
        assert result.conflict_records == []
        assert result.out_of_scope_records == []

    def test_count_properties_after_truncation(self):
        record = UserImportRecord(**_sample_record_kwargs())
        result = ImportDryRunResult(
            total=10,
            new_records=[record, record],
            conflict_records=[
                FailedRow(row_num=1, field="x", value="", reason="y", error_code="Z")
            ],
        )
        assert result.new_count == 2
        assert result.conflict_count == 1
        assert result.exists_count == 0
        assert result.out_of_scope_count == 0

    def test_files_default_none(self):
        result = ImportDryRunResult(total=0)
        assert result.conflict_records_file is None
        assert result.out_of_scope_records_file is None


class TestImportResult:
    def test_defaults(self):
        result = ImportResult(
            batch_id="b1",
            status="SUCCESS",
            success_count=5,
            failed_count=0,
        )
        assert result.skipped_count == 0
        assert result.overwritten_count == 0
        assert result.failed_rows_preview == []
        assert result.idempotent_replay is False
        assert result.failed_rows_file is None

    def test_idempotent_replay_flag(self):
        result = ImportResult(
            batch_id="b1",
            status="SUCCESS",
            success_count=0,
            failed_count=0,
            idempotent_replay=True,
        )
        assert result.idempotent_replay is True


class TestUserExportFilter:
    def test_all_optional(self):
        flt = UserExportFilter()
        assert flt.user_name is None
        assert flt.dept_id is None

    def test_status_literal(self):
        flt = UserExportFilter(status="1")
        assert flt.status == "1"

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            UserExportFilter(status="2")


class TestUserImportBatchResponse:
    def test_operator_id_serialized_as_string(self):
        resp = UserImportBatchResponse(
            batch_id="b1",
            operator_id=1234567890123456,
            filename="users.xlsx",
            file_sha256="abc",
            total_rows=100,
            preview_token="tok",
            on_conflict="skip",
            status="CREATED",
            reason="HR 同步",
            created_at=datetime(2026, 8, 3, 10, 0, 0),
        )
        # int 输入 → 序列化为 string
        serialized = resp.model_dump(by_alias=True)
        assert serialized["operatorId"] == "1234567890123456"
        assert isinstance(serialized["operatorId"], str)

    def test_camel_case_alias(self):
        resp = UserImportBatchResponse(
            batch_id="b1",
            operator_id="1",
            filename="users.xlsx",
            file_sha256="abc",
            total_rows=0,
            preview_token="tok",
            on_conflict="skip",
            status="CREATED",
            reason="HR 同步",
            created_at=datetime(2026, 8, 3, 10, 0, 0),
        )
        serialized = resp.model_dump(by_alias=True)
        assert "batchId" in serialized
        assert "previewToken" in serialized
        assert "createdAt" in serialized
        assert "totalRows" in serialized


class TestUserExportTaskResponse:
    def test_operator_id_serialized_as_string(self):
        resp = UserExportTaskResponse(
            export_id="e1",
            operator_id=9876543210987654,
            filter_snapshot={"dept_id": "1"},
            reason="导出全公司通讯录",
            status="CREATED",
            created_at=datetime(2026, 8, 3, 10, 0, 0),
        )
        serialized = resp.model_dump(by_alias=True)
        assert serialized["operatorId"] == "9876543210987654"
        assert isinstance(serialized["operatorId"], str)

    def test_optional_fields_default_none(self):
        resp = UserExportTaskResponse(
            export_id="e1",
            operator_id="1",
            filter_snapshot={},
            reason="导出",
            status="CREATED",
            created_at=datetime(2026, 8, 3, 10, 0, 0),
        )
        assert resp.row_count is None
        assert resp.file_storage_key is None
        assert resp.duration_ms is None
