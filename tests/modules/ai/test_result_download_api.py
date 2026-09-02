"""AI result downloads re-authorize the signed lineage on every read."""

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest
from tenant_helpers import tenant_context

from app.core.exceptions import NotFoundException
from app.modules.ai.api.download import download_ai_user_export
from app.modules.ai.service.result_projection_service import result_projection_service


def _user():
    return SimpleNamespace(
        user_id=101,
        user_name="alice",
        _tenant_context=tenant_context(actor_user_id=101),
    )


def _lineage():
    return result_projection_service.freeze_lineage(
        tenant=tenant_context(tenant_id=0, actor_user_id=101),
        agent_code="user_mgmt",
        tool_codes=["user.export"],
        subject_refs=[{"type": "user_export_task", "id": "exp-1"}],
        data_scope_hash="scope-1",
    )


async def test_revoked_projection_uses_not_found_surface_without_reading_file() -> None:
    with (
        patch.object(
            result_projection_service,
            "read_download_token",
            return_value=_lineage(),
        ),
        patch.object(
            result_projection_service,
            "authorize_result_projection",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.modules.ai.api.download.download_export_file",
            AsyncMock(),
        ) as download,
    ):
        with pytest.raises(NotFoundException) as exc_info:
            await download_ai_user_export(
                export_id="exp-1",
                token="signed",
                db=AsyncMock(),
                current_user=_user(),
                tenant=tenant_context(tenant_id=0, actor_user_id=101),
            )

    assert exc_info.value.error_code == "AI_RESULT_DOWNLOAD_NOT_FOUND"
    download.assert_not_awaited()


async def test_authorized_projection_reads_owner_file() -> None:
    with (
        patch.object(
            result_projection_service,
            "read_download_token",
            return_value=_lineage(),
        ),
        patch.object(
            result_projection_service,
            "authorize_result_projection",
            AsyncMock(return_value=True),
        ),
        patch(
            "app.modules.ai.api.download.download_export_file",
            AsyncMock(return_value=(b"xlsx", "users.xlsx")),
        ) as download,
    ):
        response = await download_ai_user_export(
            export_id="exp-1",
            token="signed",
            db=AsyncMock(),
            current_user=_user(),
            tenant=tenant_context(tenant_id=0, actor_user_id=101),
        )

    assert response.body == b"xlsx"
    download.assert_awaited_once_with(
        ANY,
        "exp-1",
        operator_id=101,
        allow_cross_owner=False,
        tenant=tenant_context(tenant_id=0, actor_user_id=101),
    )
