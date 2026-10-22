"""FILE tools retain private artifacts across rollback and display denial."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.exceptions import AuthorizationException
from app.core.file_storage import LocalFileStorage
from app.modules.ai.service.operation_log_service import build_target_summary
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.system.ai_tools.user_transfer import user_export
from app.modules.system.models.user_transfer import UserExportTask
from tests.modules.system.test_ai_tools_user_export import _make_ctx


def test_import_batch_target_reference_accepts_only_typed_urlsafe_identity():
    identifier = "aB_9-" + "x" * 27
    refs = [{"type": "user_import_batch", "id": identifier}]
    assert (
        build_target_summary(refs)
        == f'[{{"id":"{identifier}","type":"user_import_batch"}}]'
    )
    assert build_target_summary([{"type": "user", "id": identifier}]) is None
    assert build_target_summary([{"type": "user_import_batch", "id": "../bad"}]) is None


async def test_projection_denial_does_not_delete_a_written_file(
    db_session, tmp_path, monkeypatch
):
    storage = LocalFileStorage(tmp_path)
    monkeypatch.setattr(
        "app.modules.system.service.user_export_service.get_file_storage",
        lambda: storage,
    )
    monkeypatch.setattr(
        result_projection_service,
        "authorize_result_projection",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        result_projection_service, "issue_download_token", AsyncMock(return_value=None)
    )
    ctx = await _make_ctx(db_session)
    with pytest.raises(AuthorizationException):
        await user_export(ctx, reason="file recovery acceptance", user_names=["admin"])
    task = await db_session.scalar(
        select(UserExportTask).where(
            UserExportTask.reason == "file recovery acceptance"
        )
    )
    assert task is not None
    assert await storage.exists(task.file_storage_key)
