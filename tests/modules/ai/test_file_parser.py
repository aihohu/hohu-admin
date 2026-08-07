"""File Parser 单测 — spec §16 v1.5+ SR-24

测试 ExcelParser / CsvParser / parse_file 入口函数。

设计原则（与 keyword_blocklist / forbidden_topics 同模式）：
  - 纯函数 / 类方法测试，无 DB 依赖
  - 用 tmp_path fixture 生成临时文件
  - cell stringify / preview 截断 / 大小超限 / 编码兜底全覆盖
"""

# ruff: noqa: PLC0415

import csv
import io
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools.file_parser import (
    MAX_PARSE_CELLS,
    MAX_PARSE_COLUMNS,
    MAX_PARSE_ROWS,
    PARSERS,
    PREVIEW_ROW_LIMIT,
    SUPPORTED_MIME_TYPES,
    CsvParser,
    ExcelParser,
    parse_file,
    parse_file_bytes,
)

# ============ 辅助：构造测试文件 ============


def _make_xlsx(
    path: Path,
    rows: list[list],
    *,
    sheet_name: str = "Sheet1",
) -> None:
    """生成测试用 .xlsx，第一行为表头"""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    wb.save(str(path))
    wb.close()


def _make_csv(path: Path, rows: list[list[str]], *, encoding: str = "utf-8") -> None:
    """生成测试用 .csv，第一行为表头"""
    with path.open("w", encoding=encoding, newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


def _replace_zip_member(data: bytes, name: str, replacement: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(
                info,
                replacement if info.filename == name else source.read(info.filename),
            )
    return output.getvalue()


# ============ ExcelParser ============


class TestExcelParser:
    """ExcelParser: 解析 .xlsx，仅读 active sheet，cell stringify"""

    async def test_parse_basic(self, tmp_path: Path) -> None:
        path = tmp_path / "users.xlsx"
        _make_xlsx(
            path,
            [
                ["name", "email", "dept_id"],
                ["alice", "alice@x.com", "100"],
                ["bob", "bob@x.com", "200"],
            ],
        )
        result = await ExcelParser().parse(path)
        assert result.parser == "ExcelParser"
        assert result.rows == 2
        assert result.columns == ["name", "email", "dept_id"]
        assert len(result.preview) == 2
        assert result.preview[0] == {
            "name": "alice",
            "email": "alice@x.com",
            "dept_id": "100",
        }
        assert result.file_size > 0

    async def test_preview_truncated_to_3_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "many.xlsx"
        rows = [["id"]]
        for i in range(10):
            rows.append([i])
        _make_xlsx(path, rows)
        result = await ExcelParser().parse(path)
        assert result.rows == 10
        assert len(result.preview) == PREVIEW_ROW_LIMIT
        # 前 3 行：0, 1, 2（cell stringify → "0" / "1" / "2"）
        assert result.preview[0] == {"id": "0"}
        assert result.preview[2] == {"id": "2"}

    async def test_cell_stringify_for_datetime(self, tmp_path: Path) -> None:
        """datetime / None / 数字 一律 stringify（防 LLM 看到不支持的类型）"""
        path = tmp_path / "types.xlsx"
        dt = datetime(2026, 7, 21, 10, 30, 0)
        _make_xlsx(
            path,
            [
                ["name", "age", "joined_at", "note"],
                ["alice", 30, dt, None],
            ],
        )
        result = await ExcelParser().parse(path)
        row = result.preview[0]
        assert row["name"] == "alice"
        assert row["age"] == "30"
        assert "2026" in row["joined_at"]  # datetime stringify
        assert row["note"] == ""

    async def test_empty_header_only(self, tmp_path: Path) -> None:
        """仅有表头无数据行 → rows=0, preview=[]"""
        path = tmp_path / "header_only.xlsx"
        _make_xlsx(path, [["name", "email"]])
        result = await ExcelParser().parse(path)
        assert result.rows == 0
        assert result.columns == ["name", "email"]
        assert result.preview == []

    async def test_too_large_raises(self, tmp_path: Path) -> None:
        """超 max_bytes 抛 AI_FILE_TOO_LARGE（不实际写 50MB，patch max_bytes）"""
        path = tmp_path / "tiny.xlsx"
        _make_xlsx(path, [["a"], [1]])
        parser = ExcelParser()
        # 临时把上限设到 1 字节，触发 size 检查
        original = parser.max_bytes
        try:
            object.__setattr__(parser, "max_bytes", 1)
            with pytest.raises(BusinessRuleException) as exc_info:
                await parser.parse(path)
            assert exc_info.value.error_code == "AI_FILE_TOO_LARGE"
        finally:
            object.__setattr__(parser, "max_bytes", original)

    def test_only_modern_xlsx_mime_is_declared(self) -> None:
        assert "application/vnd.ms-excel" not in ExcelParser.mime_types
        assert (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            in ExcelParser.mime_types
        )

    async def test_invalid_xlsx_has_stable_type_error(self) -> None:
        with pytest.raises(BusinessRuleException) as exc_info:
            await ExcelParser().parse_bytes(b"PK\x03\x04damaged")
        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    async def test_malformed_worksheet_xml_has_stable_type_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "malformed-sheet.xlsx"
        _make_xlsx(path, [["name"], ["alice"]])
        data = _replace_zip_member(
            path.read_bytes(), "xl/worksheets/sheet1.xml", b"<broken"
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await ExcelParser().parse_bytes(data)

        assert exc_info.value.error_code == "AI_FILE_TYPE_NOT_ALLOWED"

    async def test_row_budget_stops_excel_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "too-many-rows.xlsx"
        _make_xlsx(path, [["id"], [1], [2], [3]])
        monkeypatch.setattr("app.modules.ai.agents.tools.file_parser.MAX_PARSE_ROWS", 2)

        with pytest.raises(BusinessRuleException) as exc_info:
            await ExcelParser().parse(path)

        assert exc_info.value.error_code == "AI_FILE_TOO_LARGE"

    async def test_column_budget_stops_excel_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "too-many-columns.xlsx"
        _make_xlsx(path, [["a", "b", "c"], [1, 2, 3]])
        monkeypatch.setattr(
            "app.modules.ai.agents.tools.file_parser.MAX_PARSE_COLUMNS", 2
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await ExcelParser().parse(path)

        assert exc_info.value.error_code == "AI_FILE_TOO_LARGE"

    def test_max_bytes_is_50mb(self) -> None:
        assert ExcelParser.max_bytes == 50 * 1024 * 1024


# ============ CsvParser ============


class TestCsvParser:
    """CsvParser: 解析 .csv（utf-8 / gbk / latin-1 兜底）"""

    async def test_parse_basic_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "users.csv"
        _make_csv(
            path,
            [
                ["name", "email"],
                ["alice", "alice@x.com"],
                ["bob", "bob@x.com"],
            ],
        )
        result = await CsvParser().parse(path)
        assert result.parser == "CsvParser"
        assert result.rows == 2
        assert result.columns == ["name", "email"]
        assert result.preview[0] == {"name": "alice", "email": "alice@x.com"}

    async def test_parse_gbk_encoding(self, tmp_path: Path) -> None:
        """Windows 导出 CSV 常为 gbk"""
        path = tmp_path / "cn.csv"
        _make_csv(
            path,
            [["姓名", "邮箱"], ["张三", "zs@x.com"]],
            encoding="gbk",
        )
        result = await CsvParser().parse(path)
        assert result.rows == 1
        assert result.columns == ["姓名", "邮箱"]
        assert result.preview[0] == {"姓名": "张三", "邮箱": "zs@x.com"}

    async def test_parse_utf8_with_bom(self, tmp_path: Path) -> None:
        """utf-8-sig (带 BOM) 不应把 BOM 当作第一列名前缀"""
        path = tmp_path / "bom.csv"
        content = "﻿name,email\nalice,a@x.com\n"
        path.write_text(content, encoding="utf-8")
        result = await CsvParser().parse(path)
        assert result.columns == ["name", "email"]
        assert result.rows == 1

    async def test_preview_truncated(self, tmp_path: Path) -> None:
        path = tmp_path / "many.csv"
        rows = [["id"]]
        for i in range(5):
            rows.append([str(i)])
        _make_csv(path, rows)
        result = await CsvParser().parse(path)
        assert result.rows == 5
        assert len(result.preview) == PREVIEW_ROW_LIMIT

    async def test_cell_budget_stops_csv_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "too-many-cells.csv"
        _make_csv(path, [["a", "b"], ["1", "2"], ["3", "4"]])
        monkeypatch.setattr(
            "app.modules.ai.agents.tools.file_parser.MAX_PARSE_CELLS", 5
        )

        with pytest.raises(BusinessRuleException) as exc_info:
            await CsvParser().parse(path)

        assert exc_info.value.error_code == "AI_FILE_TOO_LARGE"

    async def test_empty_file_returns_empty_result(self, tmp_path: Path) -> None:
        """完全空文件 → rows=0"""
        path = tmp_path / "empty.csv"
        path.write_text("", encoding="utf-8")
        result = await CsvParser().parse(path)
        assert result.rows == 0
        assert result.columns == []
        assert result.preview == []

    async def test_too_large_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "tiny.csv"
        _make_csv(path, [["a"], ["1"]])
        parser = CsvParser()
        original = parser.max_bytes
        try:
            object.__setattr__(parser, "max_bytes", 1)
            with pytest.raises(BusinessRuleException) as exc_info:
                await parser.parse(path)
            assert exc_info.value.error_code == "AI_FILE_TOO_LARGE"
        finally:
            object.__setattr__(parser, "max_bytes", original)

    def test_mime_types_declared(self) -> None:
        assert "text/csv" in CsvParser.mime_types

    def test_max_bytes_is_10mb(self) -> None:
        assert CsvParser.max_bytes == 10 * 1024 * 1024


# ============ parse_file 入口 ============


class TestParseFile:
    """parse_file 入口：MIME 路由 + 不支持类型抛错"""

    async def test_routes_xlsx_to_excel_parser(self, tmp_path: Path) -> None:
        path = tmp_path / "x.xlsx"
        _make_xlsx(path, [["a"], [1]])
        result = await parse_file(
            path,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        assert result.parser == "ExcelParser"

    async def test_routes_csv_to_csv_parser(self, tmp_path: Path) -> None:
        path = tmp_path / "x.csv"
        _make_csv(path, [["a"], ["1"]])
        result = await parse_file(path, "text/csv")
        assert result.parser == "CsvParser"

    async def test_unsupported_mime_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "x.txt"
        path.write_text("hello", encoding="utf-8")
        with pytest.raises(BusinessRuleException) as exc_info:
            await parse_file(path, "application/octet-stream")
        assert exc_info.value.error_code == "AI_FILE_TYPE_UNSUPPORTED"

    async def test_empty_mime_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "x.csv"
        _make_csv(path, [["a"], ["1"]])
        with pytest.raises(BusinessRuleException) as exc_info:
            await parse_file(path, "")
        assert exc_info.value.error_code == "AI_FILE_TYPE_UNSUPPORTED"

    async def test_parse_authorized_xlsx_bytes_without_reopening_path(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "authorized.xlsx"
        _make_xlsx(path, [["name"], ["alice"]])
        data = path.read_bytes()
        path.unlink()

        result = await parse_file_bytes(
            data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        assert result.parser == "ExcelParser"
        assert result.rows == 1
        assert result.file_size == len(data)

    async def test_parse_authorized_empty_csv_bytes(self) -> None:
        result = await parse_file_bytes(b"", "text/csv")

        assert result.parser == "CsvParser"
        assert result.rows == 0
        assert result.columns == []


# ============ PARSERS 注册表（启动一致性） ============


class TestParsersRegistry:
    """PARSERS 启动构建后不可变，所有声明 MIME 都有 parser"""

    def test_supported_mime_types_match_parsers(self) -> None:
        assert set(SUPPORTED_MIME_TYPES) == set(PARSERS.keys())

    def test_resource_budgets_are_bounded(self) -> None:
        assert MAX_PARSE_ROWS == 10_000
        assert MAX_PARSE_COLUMNS == 256
        assert MAX_PARSE_CELLS == 200_000

    def test_excel_mimes_registered(self) -> None:
        for mt in ExcelParser.mime_types:
            assert mt in PARSERS

    def test_csv_mimes_registered(self) -> None:
        for mt in CsvParser.mime_types:
            assert mt in PARSERS

    def test_no_mime_overlap_between_excel_and_csv(self) -> None:
        """ExcelParser / CsvParser 的 MIME 集合不应重叠（启动会 RuntimeError）"""
        excel_set = set(ExcelParser.mime_types)
        csv_set = set(CsvParser.mime_types)
        assert not (excel_set & csv_set), (
            f"MIME 重叠 {excel_set & csv_set}，启动 _build_parsers 会 RuntimeError"
        )
