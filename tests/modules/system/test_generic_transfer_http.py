"""Actual response-model serialization must preserve sparse task values."""

from sqlalchemy import select

from app.modules.system.models.user_transfer import UserExportTask
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)
from tests.modules.system.test_authorization_transfer_http import send_import
from tests.modules.system.test_authorization_transfer_http import (
    transfer_http as transfer_http,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def test_generic_export_http_and_history_keep_missing_cells_missing(
    transfer_http, import_sessions, monkeypatch
):
    client, _ = transfer_http
    factory, plain, _ = import_sessions
    await activate_relations(
        (factory, plain),
        monkeypatch,
        missing=(
            ("transfer.export", "row_count", "read"),
            ("transfer.export", "status", "read"),
        ),
    )
    result = await client.post("/user/export", json={"reason": "private audit reason"})
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    async with plain() as db:
        identifier = await db.scalar(select(UserExportTask.export_id))
    for path in ("/user/export", f"/user/export/{identifier}"):
        response = await client.get(path)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        row = data["records"][0] if "records" in data else data
        assert row["exportId"] == identifier
        assert "fieldPolicy" in row
        assert (
            not {
                "rowCount",
                "status",
                "filterSnapshot",
                "reason",
                "operatorId",
                "errorMessage",
            }
            & row.keys()
        )


async def test_generic_import_http_metadata_retains_zero_without_default_leaks(
    transfer_http, import_sessions, monkeypatch
):
    client, _ = transfer_http
    factory, plain, _ = import_sessions
    await activate_relations(
        (factory, plain),
        monkeypatch,
        missing=(("transfer.import", "summary_new", "read"),),
    )
    response = await send_import(client, dry_run="true")
    assert response.status_code == 200, response.text
    identifier = response.json()["data"]["batchId"]
    response = await client.get(f"/user/import/{identifier}")
    assert response.status_code == 200, response.text
    row = response.json()["data"]
    assert row["batchId"] == identifier and row["successCount"] == 0
    assert "summaryNew" not in row and "operatorId" not in row
