"""Dormant, DB-free XLSX preparation for the strict control contract."""

from dataclasses import dataclass, field
from io import BytesIO
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import ValidationError

from app.core.authorization_io import BoundedWorker
from app.core.exceptions import BusinessException, BusinessRuleException
from app.modules.system.schemas.control_config import (
    ControlConfigCreate,
    import_boolean,
)
from app.utils.closed_json import closed_object
from app.utils.safe_xlsx import UnsafeXlsxError, validate_untrusted_xlsx_xml

CONTROL_IMPORT_HEADERS = (
    "config_name",
    "config_key",
    "config_value",
    "config_type",
    "config_group",
    "status",
    "is_public",
    "remark",
)
_control_import_worker = BoundedWorker(workers=4, queue=32, timeout=3.0)


def _invalid():
    return BusinessRuleException(
        "Invalid control import workbook", error_code="CONFIG_IMPORT_INVALID_XLSX"
    )


def _limit():
    return BusinessException(
        422,
        "Control import exceeds the budget",
        error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )


def _payload(payload):
    if type(payload) is not bytes:
        raise _invalid()
    if len(payload) > 10 * 1024 * 1024:
        raise _limit()


@dataclass(frozen=True, slots=True)
class PreparedControlImport:
    """Frozen validated input only; no DB identity or execution permission."""

    records: tuple[tuple[tuple[str, object], ...], ...] = field(repr=False)


def parse_control_import(payload):
    _payload(payload)
    book = None
    try:
        validate_untrusted_xlsx_xml(payload)
        book = load_workbook(
            BytesIO(payload), read_only=True, data_only=False, keep_links=False
        )
        sheet = book.active
        if sheet is None or sheet.max_column != len(CONTROL_IMPORT_HEADERS):
            raise _invalid()
        rows = iter(sheet.iter_rows(max_col=8))
        header = next(rows)
        if tuple(cell.value for cell in header) != CONTROL_IMPORT_HEADERS or any(
            cell.data_type == "f" for cell in header
        ):
            raise _invalid()
        records, total = [], 2
        for index, cells in enumerate(rows, 1):
            if index > 1000:
                raise _limit()
            if any(cell.data_type == "f" for cell in cells):
                raise _invalid()
            raw = [cell.value for cell in cells]
            if all(value is None for value in raw):
                continue
            values = dict(zip(CONTROL_IMPORT_HEADERS, raw, strict=True))
            values["is_public"] = import_boolean(values["is_public"])
            for key, default in (("config_type", "text"), ("status", "1")):
                if values[key] is None or values[key] == "":
                    values[key] = default
            model = ControlConfigCreate.model_validate(values)
            normalized = model.model_dump()
            _, encoded = closed_object(normalized, max_bytes=16 * 1024)
            total += len(encoded) + (1 if records else 0)
            if total > 1024 * 1024:
                raise _limit()
            records.append(tuple(normalized.items()))
        return PreparedControlImport(tuple(records))
    except (
        UnsafeXlsxError,
        InvalidFileException,
        BadZipFile,
        ValueError,
        OSError,
        EOFError,
        IndexError,
        KeyError,
        RuntimeError,
        SyntaxError,
        TypeError,
        StopIteration,
        ValidationError,
    ):
        raise _invalid() from None
    finally:
        if book is not None:
            book.close()


async def prepare_control_import(payload):
    _payload(payload)
    return await _control_import_worker.run(parse_control_import, payload)
