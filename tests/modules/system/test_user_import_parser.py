"""parse_import_excel 单测（Task 8，spec §3.6 line 2036 + line 2624-2630）。

覆盖：
- MIME 白名单（xlsx/xls/csv）→ AI_IMPORT_INVALID_MIME
- 文件大小 ≤ 10MB → AI_IMPORT_FILE_TOO_LARGE
- 行数 ≤ 2000 → AI_IMPORT_TOO_MANY_ROWS
- 字段校验：user_name 必填 / 邮箱 / 手机 / gender / status / 长度
- 失败行一次性收集（ImportErrorCollection，spec §2.12）
- 空字符串 employee_no 规范化为 None（spec §2.24）
- 可选字段默认值（gender="0" / status="1"）

不依赖 DB（解析层零 DB 查询，dept_input / role_input 存在性留给 dry_run）。
"""

import io

import pytest
from openpyxl import Workbook

from app.core.exceptions import BusinessRuleException
from app.modules.system.user.constants import USER_IMPORT_MAX_ROWS
from app.modules.system.user.import_parser import (
    ALLOWED_MIME_TYPES,
    MAX_FILE_SIZE_BYTES,
    ImportErrorCollection,
    parse_import_excel,
)
from app.modules.system.user.schemas import FailedRow, UserImportRecord


def _xlsx_bytes(rows: list[list[str]], headers: list[str] | None = None) -> bytes:
    """构造 xlsx 文件 bytes（含表头 + 数据行）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "数据"
    if headers is None:
        headers = [
            "user_name",
            "employee_no",
            "nickname",
            "user_email",
            "user_phone",
            "dept_input",
            "role_input",
            "user_gender",
            "status",
        ]
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv_bytes(rows: list[list[str]], headers: list[str] | None = None) -> bytes:
    """构造 CSV 文件 bytes。"""
    if headers is None:
        headers = [
            "user_name",
            "employee_no",
            "nickname",
            "user_email",
            "user_phone",
            "dept_input",
            "role_input",
            "user_gender",
            "status",
        ]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(row))
    return "\n".join(lines).encode("utf-8")


MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_CSV = "text/csv"
MIME_INVALID = "application/pdf"


class TestMimeTypeWhitelist:
    """spec §2.10：MIME 白名单 {xlsx, xls, csv}。"""

    def test_allowed_mime_types_includes_xlsx_xls_csv(self):
        assert MIME_XLSX in ALLOWED_MIME_TYPES
        assert "application/vnd.ms-excel" in ALLOWED_MIME_TYPES
        assert MIME_CSV in ALLOWED_MIME_TYPES

    def test_invalid_mime_raises(self):
        with pytest.raises(BusinessRuleException) as exc:
            parse_import_excel(b"whatever", MIME_INVALID)
        assert exc.value.error_code == "AI_IMPORT_INVALID_MIME"


class TestFileSizeLimit:
    """spec §2.10：≤ 10MB。"""

    def test_max_file_size_is_10mb(self):
        assert MAX_FILE_SIZE_BYTES == 10 * 1024 * 1024

    def test_too_large_raises(self):
        # 攒 10MB + 1 byte
        bogus = b"x" * (MAX_FILE_SIZE_BYTES + 1)
        with pytest.raises(BusinessRuleException) as exc:
            parse_import_excel(bogus, MIME_XLSX)
        assert exc.value.error_code == "AI_IMPORT_FILE_TOO_LARGE"


class TestRowCountLimit:
    """spec §2.10：行数 ≤ 2000（v2.2 P0）。"""

    def test_user_import_max_rows_is_2000(self):
        assert USER_IMPORT_MAX_ROWS == 2000

    def test_too_many_rows_raises(self):
        rows = [
            [f"user{i}", "", f"nick{i}", "", "", "QA-Dept", "", "0", "1"]
            for i in range(USER_IMPORT_MAX_ROWS + 1)
        ]
        with pytest.raises(BusinessRuleException) as exc:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        assert exc.value.error_code == "AI_IMPORT_TOO_MANY_ROWS"


class TestParseXlsxBasic:
    """spec line 2624：标准 xlsx → records 数正确。"""

    def test_parse_xlsx_basic(self):
        rows = [
            [
                "alice",
                "E001",
                "Alice",
                "alice@example.com",
                "13800138000",
                "QA-Dept",
                "R_DEV",
                "1",
                "1",
            ],
            [
                "bob",
                "E002",
                "Bob",
                "bob@example.com",
                "13900139000",
                "QA-Dept",
                "",
                "0",
                "1",
            ],
        ]
        records = parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        assert len(records) == 2
        assert isinstance(records[0], UserImportRecord)
        assert records[0].row_num == 2  # 表头是 row 1
        assert records[0].user_name == "alice"
        assert records[0].employee_no == "E001"
        assert records[0].nickname == "Alice"
        assert records[0].user_email == "alice@example.com"
        assert records[0].user_phone == "13800138000"
        assert records[0].dept_input == "QA-Dept"
        assert records[0].role_input == "R_DEV"
        assert records[0].user_gender == "1"
        assert records[0].status == "1"

    def test_parse_csv_basic(self):
        rows = [
            [
                "carol",
                "",
                "Carol",
                "carol@example.com",
                "13700137000",
                "QA-Dept",
                "",
                "0",
                "1",
            ],
        ]
        records = parse_import_excel(_csv_bytes(rows), MIME_CSV)
        assert len(records) == 1
        assert records[0].user_name == "carol"
        assert records[0].employee_no is None  # 空串 → None（spec §2.24）

    def test_parse_skips_empty_rows(self):
        rows = [
            ["alice", "", "Alice", "", "", "QA-Dept", "", "0", "1"],
            ["", "", "", "", "", "", "", "", ""],  # 全空行
            ["bob", "", "Bob", "", "", "QA-Dept2", "", "0", "1"],
        ]
        records = parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        assert len(records) == 2
        assert {r.user_name for r in records} == {"alice", "bob"}


class TestOptionalFieldsDefaults:
    """可选字段缺省时的默认值（gender="0" / status="1"）。"""

    def test_gender_defaults_to_zero(self):
        rows = [["dave", "", "Dave", "", "", "QA-Dept", "", "", ""]]
        records = parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        assert records[0].user_gender == "0"
        assert records[0].status == "1"  # 默认启用

    def test_employee_no_blank_normalized_to_none(self):
        """spec §2.24 line 839：normalize_employee_no 空 → None（避免 UNIQUE 冲突）。"""
        rows = [["eve", "   ", "Eve", "", "", "QA-Dept", "", "0", "1"]]
        records = parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        assert records[0].employee_no is None

    def test_role_input_blank_normalized_to_none(self):
        rows = [["frank", "", "Frank", "", "", "QA-Dept", "   ", "0", "1"]]
        records = parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        assert records[0].role_input is None


class TestFieldValidationCollectsErrors:
    """spec §2.12 + line 2046：失败行收集 → ImportErrorCollection（不一次一个）。"""

    def test_missing_required_user_name_collected(self):
        rows = [
            ["", "", "NoName", "", "", "QA-Dept", "", "0", "1"],
        ]
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        assert len(errors) == 1
        assert errors[0].row_num == 2
        assert errors[0].field == "user_name"
        assert errors[0].error_code == "AI_IMPORT_USERNAME_INVALID"

    def test_missing_required_dept_input_collected(self):
        rows = [
            ["alice", "", "Alice", "", "", "", "", "0", "1"],
        ]
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        assert any(
            e.field == "dept_input" and e.error_code == "AI_IMPORT_DEPT_INPUT_REQUIRED"
            for e in errors
        )

    def test_user_name_too_short_collected(self):
        rows = [["a", "", "A", "", "", "QA-Dept", "", "0", "1"]]  # 1 字符 < 2
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        assert any(e.field == "user_name" for e in errors)

    def test_user_name_too_long_collected(self):
        rows = [["a" * 17, "", "Long", "", "", "QA-Dept", "", "0", "1"]]
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        assert any(e.field == "user_name" for e in errors)

    def test_email_format_invalid_collected(self):
        """spec line 2630：邮箱格式错 → AI_IMPORT_EMAIL_INVALID。"""
        rows = [["alice", "", "Alice", "not-an-email", "", "QA-Dept", "", "0", "1"]]
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        assert len(errors) == 1
        assert errors[0].field == "user_email"
        assert errors[0].error_code == "AI_IMPORT_EMAIL_INVALID"

    def test_phone_format_invalid_collected(self):
        rows = [["alice", "", "Alice", "", "12345", "QA-Dept", "", "0", "1"]]
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        assert len(errors) == 1
        assert errors[0].field == "user_phone"
        assert errors[0].error_code == "AI_IMPORT_PHONE_INVALID"

    def test_phone_valid_mainland_format_passes(self):
        rows = [["alice", "", "Alice", "", "13800138000", "QA-Dept", "", "0", "1"]]
        records = parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        assert records[0].user_phone == "13800138000"

    def test_gender_invalid_collected(self):
        rows = [["alice", "", "Alice", "", "", "QA-Dept", "", "9", "1"]]
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        assert any(
            e.field == "user_gender" and e.error_code == "AI_IMPORT_GENDER_INVALID"
            for e in errors
        )

    def test_status_invalid_collected(self):
        rows = [["alice", "", "Alice", "", "", "QA-Dept", "", "0", "9"]]
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        assert any(
            e.field == "status" and e.error_code == "AI_IMPORT_STATUS_INVALID"
            for e in errors
        )

    def test_collects_multiple_errors_across_rows(self):
        """spec §2.12：一次收集所有错误（不让用户改一行重传一次）。"""
        rows = [
            ["", "", "NoName", "", "", "QA-Dept", "", "0", "1"],  # row 2 user_name 缺
            [
                "bob",
                "",
                "Bob",
                "bad-email",
                "",
                "QA-Dept",
                "",
                "0",
                "1",
            ],  # row 3 email 错
            [
                "carol",
                "",
                "Carol",
                "",
                "12345",
                "QA-Dept",
                "",
                "0",
                "1",
            ],  # row 4 phone 错
            ["dave", "", "Dave", "", "", "QA-Dept", "", "0", "1"],  # row 5 OK
        ]
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        # 3 行各 1 个错误，row 5 OK
        assert len(errors) == 3
        row_nums = {e.row_num for e in errors}
        assert row_nums == {2, 3, 4}
        # row_num 升序
        assert [e.row_num for e in errors] == sorted(e.row_num for e in errors)

    def test_single_row_multiple_errors_collected(self):
        """同一行多个字段错 → 全部收集（user_name 缺 + email 错 + phone 错）。"""
        rows = [["", "", "X", "bad-email", "12345", "", "", "9", "9"]]
        with pytest.raises(ImportErrorCollection) as exc_info:
            parse_import_excel(_xlsx_bytes(rows), MIME_XLSX)
        errors = exc_info.value.errors
        fields = {e.field for e in errors}
        assert "user_name" in fields
        assert "user_email" in fields
        assert "user_phone" in fields
        assert "dept_input" in fields
        assert "user_gender" in fields
        assert "status" in fields


class TestImportErrorCollection:
    """ImportErrorCollection 异常行为（spec §2.12 + §3.6 line 2046）。"""

    def test_is_exception_subclass(self):
        exc = ImportErrorCollection(
            errors=[
                FailedRow(
                    row_num=2,
                    field="user_name",
                    value="",
                    reason="必填",
                    error_code="AI_IMPORT_USERNAME_INVALID",
                )
            ]
        )
        assert isinstance(exc, Exception)
        assert len(exc.errors) == 1
        assert "1 field errors" in str(exc)
