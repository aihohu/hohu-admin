"""Dormant import preparation validates full records before any owner or SQL."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.core.exceptions import BusinessException
from app.modules.system.schemas.control_config import (
    ControlConfigCreate,
    ControlConfigUpdate,
    import_boolean,
)
from app.modules.system.service import control_config_input as inputs

HEADERS = (
    "config_name",
    "config_key",
    "config_value",
    "config_type",
    "config_group",
    "status",
    "is_public",
    "remark",
)


def workbook(rows, *, headers=HEADERS):
    book, output = Workbook(), BytesIO()
    book.active.append(headers)
    for row in rows:
        book.active.append(row)
    book.save(output)
    book.close()
    return output.getvalue()


@pytest.mark.parametrize(
    "raw,expected",
    [
        (False, False),
        (True, True),
        (0, False),
        (1, True),
        ("false", False),
        ("true", True),
        ("0", False),
        ("1", True),
        (None, False),
    ],
)
def test_import_boolean_is_not_python_string_truthiness(raw, expected):
    assert import_boolean(raw) is expected


@pytest.mark.parametrize("raw", ["no", "yes", "arbitrary", 2, [], {}, 0.0])
def test_ambiguous_boolean_input_is_rejected(raw):
    with pytest.raises(BusinessException):
        import_boolean(raw)


def test_unknown_internal_columns_and_empty_update_are_rejected():
    valid = {
        "config_name": "Name",
        "config_key": "key",
        "config_value": "value",
        "config_type": "text",
        "config_group": "group",
        "status": "1",
    }
    assert ControlConfigCreate.model_validate(valid).is_public is False
    assert ControlConfigUpdate.model_validate({"remark": None}).model_fields_set == {
        "remark"
    }
    for changes in (
        {"value_sensitivity": "private"},
        {"publication_digest": "0" * 64},
        {"published_by_user_id": "1"},
    ):
        with pytest.raises(ValidationError):
            ControlConfigCreate.model_validate(valid | changes)
    for payload in ({}, {"config_value": None}, {"config_key": None}, {"unknown": "x"}):
        with pytest.raises((ValidationError, BusinessException)):
            ControlConfigUpdate.model_validate(payload)
    for schema, payload in (
        (ControlConfigCreate, valid | {"is_public": True}),
        (ControlConfigUpdate, {"is_public": True}),
    ):
        with pytest.raises((ValidationError, BusinessException)):
            schema.model_validate(payload)


async def test_import_is_frozen_and_false_is_not_published():
    payload = workbook([("N", "ordinary", "V", "text", "group", "1", "false", None)])
    prepared = await inputs.prepare_control_import(payload)
    assert prepared.records == (
        (
            ("config_name", "N"),
            ("config_key", "ordinary"),
            ("config_value", "V"),
            ("config_type", "text"),
            ("config_group", "group"),
            ("status", "1"),
            ("is_public", False),
            ("remark", None),
        ),
    )
    assert "ordinary" not in repr(prepared)


@pytest.mark.parametrize(
    "headers",
    [
        (*HEADERS[:-1], "value_sensitivity"),
        (*HEADERS, "extra"),
        ("config_key", "config_name", *HEADERS[2:]),
        (*HEADERS[:-1], "config_key"),
    ],
)
async def test_unregistered_or_ambiguous_headers_are_rejected(headers):
    with pytest.raises(BusinessException):
        await inputs.prepare_control_import(workbook([], headers=headers))


@pytest.mark.parametrize(
    "row",
    [
        ("N", "ordinary", "V", "text", "group", "1", True, None),
        (
            "N",
            "ordinary",
            '{"inner":{"secret":"sentinel"}}',
            "text",
            "group",
            "1",
            False,
            None,
        ),
        (
            "N",
            "ordinary",
            '=HYPERLINK("https://example.test")',
            "text",
            "group",
            "1",
            False,
            None,
        ),
        ("N", "ordinary", "V", "unknown", "group", "1", False, None),
        ("N", "ordinary", "V", "text", "group", "unexpected", False, None),
    ],
)
async def test_import_does_not_bypass_publication_schema_or_formula_guards(row):
    with pytest.raises(BusinessException) as error:
        await inputs.prepare_control_import(workbook([row]))
    assert "sentinel" not in str(error.value)


@pytest.mark.parametrize("column", [3, 5])
async def test_false_is_not_an_empty_type_or_status(column):
    row = ["N", "ordinary", "V", "text", "group", "1", False, None]
    row[column] = False
    with pytest.raises(BusinessException):
        await inputs.prepare_control_import(workbook([row]))


@pytest.mark.parametrize("count,value", [(1001, "V"), (128, "x" * 8192)])
def test_decoded_row_and_aggregate_limits_are_enforced_independently_of_zip_budget(
    monkeypatch, count, value
):
    rows = [HEADERS] + [
        ("N", f"key{i}", value, "text", "group", "1", False, None) for i in range(count)
    ]
    cells = [
        tuple(SimpleNamespace(value=value, data_type="s") for value in row)
        for row in rows
    ]
    book = Mock()
    book.active.max_column = 8
    book.active.iter_rows.return_value = iter(cells)
    monkeypatch.setattr(inputs, "validate_untrusted_xlsx_xml", lambda _value: None)
    monkeypatch.setattr(inputs, "load_workbook", lambda *_args, **_kwargs: book)
    with pytest.raises(BusinessException) as error:
        inputs.parse_control_import(b"already preflighted bytes")
    assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    book.close.assert_called_once()


async def test_physical_rows_individual_values_aggregate_bytes_and_file_caps():
    base = ("N", "key", "V", "text", "group", "1", False, None)
    payloads = [
        workbook([base] * 1001),
        workbook([(*base[:2], "汉" * 2731, *base[3:])]),
        workbook([("N", f"key{i}", "x" * 8192, *base[3:]) for i in range(128)]),
        b"x" * (10 * 1024 * 1024 + 1),
        b"not xlsx",
    ]
    for payload in payloads:
        with pytest.raises(BusinessException):
            await inputs.prepare_control_import(payload)


def test_workbook_is_closed_when_iteration_fails(monkeypatch):
    book = Mock()
    book.active.max_column = 8
    book.active.iter_rows.side_effect = ValueError("private value must not escape")
    monkeypatch.setattr(inputs, "validate_untrusted_xlsx_xml", lambda _value: None)
    monkeypatch.setattr(inputs, "load_workbook", lambda *_args, **_kwargs: book)
    with pytest.raises(BusinessException) as error:
        inputs.parse_control_import(b"test")
    assert "private value" not in str(error.value)
    book.close.assert_called_once()
