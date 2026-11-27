"""AI download compatibility: revoked grants and legacy owner files."""

from unittest.mock import AsyncMock

from sqlalchemy import update

from app.core.file_storage import LocalFileStorage
from app.modules.ai.models.agent import AiAgent
from app.modules.system.models.user_transfer import UserExportTask
from tests.modules.ai.test_authorization_download_http import (
    signed_export as signed_export,
)
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)
from tests.modules.system.test_authorization_transfer_http import (
    transfer_http as transfer_http,
)


async def test_revoked_projection_uses_not_found_surface_without_reading_file(
    signed_export, monkeypatch
):
    client, plain, identifier, token, _ = signed_export
    async with plain() as db:
        await db.execute(
            update(AiAgent).where(AiAgent.agent_id == 901).values(enabled=False)
        )
        await db.commit()
    read = AsyncMock(side_effect=AssertionError("Denied projection must not read FILE"))
    monkeypatch.setattr(LocalFileStorage, "read_artifact", read)
    response = await client.get(
        f"/ai/download/user-export/{identifier}", params={"token": token}
    )
    assert response.status_code == 404
    assert response.json()["errorCode"] == "AI_RESULT_DOWNLOAD_NOT_FOUND"
    read.assert_not_awaited()


async def test_authorized_projection_reads_owner_file(signed_export, tmp_path):
    client, plain, identifier, token, data = signed_export
    storage = LocalFileStorage(tmp_path)
    path = await storage.save(
        data,
        namespace="exports",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        suffix=".xlsx",
    )
    async with plain() as db:
        await db.execute(
            update(UserExportTask)
            .where(UserExportTask.export_id == identifier)
            .values(file_storage_key=path)
        )
        await db.commit()
    response = await client.get(
        f"/ai/download/user-export/{identifier}", params={"token": token}
    )
    assert response.status_code == 200, response.text
    assert response.content == data
