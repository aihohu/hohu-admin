"""Novice spreadsheet headers use the same parsing and permission contract."""

from io import BytesIO

import pytest
from openpyxl import Workbook

from app.core.exceptions import BusinessRuleException
from app.modules.system.service.user_import_parser import (
    MIME_CSV,
    MIME_XLSX,
    ImportErrorCollection,
    import_file_has_column,
    parse_import_excel,
)


@pytest.mark.parametrize("mime", [MIME_CSV, MIME_XLSX])
def test_chinese_headers_and_disabled_label_match_role_permission_detection(mime):
    rows = [
        ["用户名", "昵称", "邮箱", "部门", "角色", "状态"],
        ["novice0915", "新同事", "novice@example.com", "华南", "普通用户", "停用"],
    ]
    if mime == MIME_CSV:
        data = "\n".join(",".join(row) for row in rows).encode("utf-8-sig")
    else:
        book = Workbook()
        for row in rows:
            book.active.append(row)
        stream = BytesIO()
        book.save(stream)
        data = stream.getvalue()
    assert import_file_has_column(data, mime, "role_input")
    record = parse_import_excel(data, mime)[0]
    assert record.user_name == "novice0915"
    assert record.role_input == "普通用户"
    assert record.status == "2"


def test_ambiguous_alias_columns_are_rejected():
    with pytest.raises(BusinessRuleException, match="重复"):
        parse_import_excel("用户名,user_name\na,b".encode(), MIME_CSV)


def test_reserved_email_domain_is_rejected_before_import():
    with pytest.raises(ImportErrorCollection) as exc:
        parse_import_excel(
            b"user_name,user_email,dept_input\nnovice,novice@example.test,Dept",
            MIME_CSV,
        )
    assert exc.value.errors[0].error_code == "AI_IMPORT_EMAIL_INVALID"
