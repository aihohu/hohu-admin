from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import select

from app.modules.system.api.file import router as file_router
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.models.user_transfer import UserImportBatch
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)
from tests.modules.system.test_authorization_transfer_http import send_import
from tests.modules.system.test_authorization_transfer_http import (
    transfer_http as transfer_http,
)


async def test_error_download_uses_final_epoch_and_only_safe_cells(transfer_http):
    client, plain = transfer_http
    client._transport.app.include_router(file_router, prefix="/system/file")
    raw = b"user_name,dept_input\ngoodemployee,Team\nsecretemployee,secretdept\n"
    preview = await send_import(client, raw=raw, dry_run="true")
    assert preview.status_code == 200, preview.text
    result = await send_import(
        client, raw=raw, preview_token=preview.json()["data"]["previewToken"]
    )
    assert result.status_code == 200, result.text
    body = result.json()["data"]
    assert body["status"] == "PARTIAL_SUCCESS"
    assert body["successCount"] == 1 and body["failedCount"] == 1
    assert "secret" not in str(body["failedRowsPreview"])
    assert body["failedRowsFile"].startswith("/system/file/import-error/")
    async with plain() as db:
        batch = await db.get(UserImportBatch, body["batchId"])
        epoch = await db.scalar(select(UserFieldPolicyState.epoch))
        assert (
            batch.field_lineage["import_errors"]["stamp"]["tenant_epoch"] == epoch == 3
        )
    download = await client.get(body["failedRowsFile"])
    assert download.status_code == 200, download.text
    assert download.headers["cache-control"] == "no-store"
    wb = load_workbook(BytesIO(download.content))
    assert "secret" not in str([cell.value for ws in wb for row in ws for cell in row])
    wb.close()
