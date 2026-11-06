"""Transfer field presence and storage contracts must be explicit."""

from io import BytesIO

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.core.exceptions import BusinessRuleException
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch
from app.modules.system.schemas.user_transfer import UserExportRequest
from app.modules.system.service.user_import_parser import MIME_CSV, parse_import_excel


@pytest.mark.parametrize(
    "mime",
    ["text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
)
def test_nonempty_unlabelled_cells_are_not_silently_outside_the_write_plan(mime):
    if mime == "text/csv":
        data = b"user_name,dept_input,\nreader,Engineering,secret-unlabelled\n"
    else:
        wb = Workbook()
        wb.active.append(["user_name", "dept_input"])
        wb.active.append(["reader", "Engineering", "secret-unlabelled"])
        output = BytesIO()
        wb.save(output)
        wb.close()
        data = output.getvalue()
    with pytest.raises(BusinessRuleException) as caught:
        parse_import_excel(data, mime)
    assert caught.value.error_code == "AI_IMPORT_UNKNOWN_HEADER"
    assert "secret-unlabelled" not in str(caught.value)


def test_import_presence_distinguishes_absent_and_empty_columns():
    record = parse_import_excel(
        b"user_name,dept_input,nickname\nreader,Engineering,\n", MIME_CSV
    )[0]
    assert record.nickname is None
    assert record.model_fields_set == {"row_num", "user_name", "dept_input", "nickname"}
    assert record.user_gender == "0"
    assert record.status == "1"


@pytest.mark.parametrize("column", ["hashed_password", "tenant_id", "unknown"])
def test_import_unknown_columns_are_not_silently_discarded(column):
    data = f"user_name,dept_input,{column}\nreader,Engineering,secret\n".encode()
    with pytest.raises(BusinessRuleException) as raised:
        parse_import_excel(data, MIME_CSV)
    assert raised.value.error_code == "AI_IMPORT_UNKNOWN_HEADER"
    assert "secret" not in str(raised.value)


def test_export_rejects_unknown_and_ambiguous_filter_fields():
    with pytest.raises(ValidationError):
        UserExportRequest(reason="audit", hidden_filter="secret")
    with pytest.raises(ValidationError):
        UserExportRequest(reason="audit", user_name="reader", userName="reader")


@pytest.mark.parametrize("model", [UserImportBatch, UserExportTask])
def test_transfer_lineage_is_nullable_and_has_no_historical_default(model):
    assert "field_lineage" in model.__table__.c
    column = model.__table__.c.field_lineage
    assert column.nullable
    assert column.default is None and column.server_default is None
