"""Security boundary tests for untrusted configuration workbooks."""

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile
from openpyxl import Workbook

from app.core.exceptions import BusinessRuleException
from app.modules.system.api.config import (
    CONFIG_IMPORT_MAX_SIZE_BYTES,
)
from app.modules.system.api.config import (
    import_configs as import_configs_api,
)
from app.modules.system.service.config_service import config_service


def _malicious_xlsx() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["config_name", "config_key"])
    source_buffer = io.BytesIO()
    workbook.save(source_buffer)
    workbook.close()

    source = zipfile.ZipFile(io.BytesIO(source_buffer.getvalue()))
    output = io.BytesIO()
    malicious_xml = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE worksheet [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
        b"<worksheet>&secret;</worksheet>"
    )
    with source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(
                info,
                malicious_xml
                if info.filename == "xl/worksheets/sheet1.xml"
                else source.read(info.filename),
            )
    return output.getvalue()


async def test_config_import_rejects_dtd_and_external_entity_before_database() -> None:
    database = AsyncMock()

    with pytest.raises(BusinessRuleException) as exc_info:
        await config_service.import_configs(
            database,
            _malicious_xlsx(),
            tenant=MagicMock(),
        )

    assert exc_info.value.error_code == "CONFIG_IMPORT_INVALID_XLSX"
    database.execute.assert_not_awaited()


async def test_config_import_upload_uses_bounded_read_before_service() -> None:
    upload = MagicMock(spec=UploadFile)
    upload.read = AsyncMock(return_value=b"x" * (CONFIG_IMPORT_MAX_SIZE_BYTES + 1))
    database = AsyncMock()
    tenant = MagicMock(tenant_id=0)

    with pytest.raises(BusinessRuleException) as exc_info:
        await import_configs_api(file=upload, db=database, tenant=tenant)

    assert exc_info.value.error_code == "CONFIG_IMPORT_FILE_TOO_LARGE"
    upload.read.assert_awaited_once_with(CONFIG_IMPORT_MAX_SIZE_BYTES + 1)
    database.commit.assert_not_awaited()
