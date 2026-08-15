"""P1-D authorization-lineage and fail-closed projection tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jose import JWTError, jwt

from app.constants import STATUS_ENABLED
from app.core.config import settings
from app.modules.ai.service.result_projection_service import (
    DATA_SCOPE_RESOLVER_VERSION,
    ProjectionLineage,
    result_projection_service,
)


def _user(*permissions: str):
    role = SimpleNamespace(
        role_id=11,
        role_code="R_USER",
        status=STATUS_ENABLED,
        data_scope="1",
        depts=[],
        menus=[SimpleNamespace(permission=value) for value in permissions],
    )
    return SimpleNamespace(
        user_id=101,
        user_name="alice",
        roles=[role],
        depts=[],
    )


def test_freeze_lineage_normalizes_sets_and_hashes_subjects() -> None:
    lineage = result_projection_service.freeze_lineage(
        tenant_id=0,
        agent_code="user_mgmt",
        tool_codes=["user.lookup", "user.lookup"],
        subject_refs=[
            {"type": "user", "id": "9"},
            {"id": "2", "type": "user"},
            {"type": "user", "id": "9"},
        ],
    )

    assert lineage.tool_codes == ("user.lookup",)
    assert lineage.subject_refs == (
        {"type": "user", "id": "2"},
        {"type": "user", "id": "9"},
    )
    assert len(lineage.subject_refs_hash) == 64
    assert lineage.resolver_version == DATA_SCOPE_RESOLVER_VERSION


async def test_projection_rejects_tampered_subject_hash_before_domain_queries() -> None:
    user = _user("ai:chat:use", "system:user:list")
    lineage = ProjectionLineage(
        tenant_id=0,
        agent_code="user_mgmt",
        tool_codes=("user.lookup",),
        subject_refs=({"type": "user", "id": "9"},),
        subject_refs_hash="0" * 64,
        data_scope_hash=None,
        resolver_version=DATA_SCOPE_RESOLVER_VERSION,
    )

    with patch.object(
        result_projection_service,
        "_authorize_agent_and_tools",
        AsyncMock(),
    ) as authorize_tools:
        allowed = await result_projection_service.authorize_result_projection(
            AsyncMock(),
            user,
            owner_user_id=user.user_id,
            lineage=lineage,
        )

    assert allowed is False
    authorize_tools.assert_not_awaited()


async def test_projection_requires_owner_tenant_and_current_chat_permission() -> None:
    lineage = result_projection_service.freeze_lineage(
        tenant_id=0,
        agent_code="user_mgmt",
        tool_codes=[],
        subject_refs=[],
    )

    assert not await result_projection_service.authorize_result_projection(
        AsyncMock(),
        _user(),
        owner_user_id=101,
        lineage=lineage,
    )
    assert not await result_projection_service.authorize_result_projection(
        AsyncMock(),
        _user("ai:chat:use"),
        owner_user_id=999,
        lineage=lineage,
    )


async def test_projection_accepts_complete_empty_lineage_for_plain_assistant_text() -> (
    None
):
    user = _user("ai:chat:use")
    lineage = result_projection_service.freeze_lineage(
        tenant_id=0,
        agent_code="shared",
        tool_codes=[],
        subject_refs=[],
    )

    with patch.object(
        result_projection_service,
        "_authorize_agent_and_tools",
        AsyncMock(return_value=True),
    ):
        allowed = await result_projection_service.authorize_result_projection(
            AsyncMock(),
            user,
            owner_user_id=user.user_id,
            lineage=lineage,
        )

    assert allowed is True


async def test_scope_bound_projection_requires_exact_hash_and_resolver_version() -> (
    None
):
    user = _user("ai:chat:use")
    lineage = result_projection_service.freeze_lineage(
        tenant_id=0,
        agent_code="user_mgmt",
        tool_codes=["user.count"],
        subject_refs=[],
        data_scope_hash="frozen-scope",
    )

    with (
        patch.object(
            result_projection_service,
            "_authorize_agent_and_tools",
            AsyncMock(return_value=True),
        ),
        patch.object(
            result_projection_service,
            "compute_data_scope_hash",
            AsyncMock(return_value="current-scope"),
        ),
    ):
        allowed = await result_projection_service.authorize_result_projection(
            AsyncMock(),
            user,
            owner_user_id=user.user_id,
            lineage=lineage,
        )

    assert allowed is False


async def test_data_scope_hash_tracks_resolved_department_sets() -> None:
    user = _user("ai:chat:use")
    db = AsyncMock()
    first_scope = SimpleNamespace(
        accessible_dept_ids={10, 20},
        accessible_user_scope=object(),
    )
    second_scope = SimpleNamespace(
        accessible_dept_ids={10, 30},
        accessible_user_scope=object(),
    )

    first = await result_projection_service.compute_data_scope_hash(
        db,
        user,
        data_scope=first_scope,
    )
    second = await result_projection_service.compute_data_scope_hash(
        db,
        user,
        data_scope=second_scope,
    )

    assert first != second


async def test_download_token_is_owner_resource_and_projection_bound() -> None:
    user = _user("ai:chat:use")
    other_user = _user("ai:chat:use")
    other_user.user_id = 999
    lineage = result_projection_service.freeze_lineage(
        tenant_id=0,
        agent_code="user_mgmt",
        tool_codes=["user.export"],
        subject_refs=[{"type": "user_export_task", "id": "exp-1"}],
        data_scope_hash="scope-1",
    )

    with patch.object(
        result_projection_service,
        "authorize_result_projection",
        AsyncMock(return_value=True),
    ):
        token = await result_projection_service.issue_download_token(
            AsyncMock(),
            user,
            resource_type="user_export",
            resource_id="exp-1",
            lineage=lineage,
        )

    assert token is not None
    with pytest.raises(JWTError):
        jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
    assert (
        result_projection_service.read_download_token(
            token,
            user,
            resource_type="user_export",
            resource_id="exp-1",
        )
        == lineage
    )
    assert (
        result_projection_service.read_download_token(
            token,
            other_user,
            resource_type="user_export",
            resource_id="exp-1",
        )
        is None
    )
    assert (
        result_projection_service.read_download_token(
            token,
            user,
            resource_type="user_export",
            resource_id="exp-2",
        )
        is None
    )


async def test_refresh_download_urls_replaces_persisted_token() -> None:
    user = _user("ai:chat:use")
    lineage = result_projection_service.freeze_lineage(
        tenant_id=0,
        agent_code="user_mgmt",
        tool_codes=["user.export"],
        subject_refs=[{"type": "user_export_task", "id": "exp-1"}],
        data_scope_hash="scope-1",
    )
    with patch.object(
        result_projection_service,
        "issue_download_token",
        AsyncMock(return_value="fresh-token"),
    ):
        refreshed = await result_projection_service.refresh_download_urls(
            AsyncMock(),
            user,
            lineage=lineage,
            value={
                "downloadUrl": ("/ai/download/user-export/exp-1?token=expired-token")
            },
        )

    assert refreshed["downloadUrl"].endswith("token=fresh-token")
