"""Query-cache lookups use one indistinguishable not-found surface."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from tenant_helpers import tenant_context

from app.core.exceptions import NotFoundException
from app.modules.ai.api.query_cache import get_query_cache_endpoint


def _user():
    return SimpleNamespace(
        user_id=101,
        user_name="alice",
        _tenant_context=tenant_context(actor_user_id=101),
    )


def _entry(*, user_id: int = 101):
    return SimpleNamespace(
        module="system/user",
        filters={"status": "1"},
        tool_name="user.list",
        user_id=user_id,
        tenant_id=0,
        agent_code="user_mgmt",
        tool_codes=["user.list"],
        subject_refs=[],
        subject_refs_hash="a" * 64,
        data_scope_hash=None,
        resolver_version="legacy-max-v1",
        projection_dependency_message_ids=[],
        schema_version=3,
        created_at="2026-08-15T09:00:00Z",
    )


@pytest.mark.parametrize("entry", [None, _entry(user_id=999), _entry()])
async def test_missing_owner_mismatch_and_revoked_projection_share_404(entry) -> None:
    with (
        patch(
            "app.modules.ai.api.query_cache.get_query_cache",
            AsyncMock(return_value=entry),
        ),
        patch(
            "app.modules.ai.api.query_cache.result_projection_service.authorize_result_projection",
            AsyncMock(return_value=False),
        ),
    ):
        with pytest.raises(NotFoundException) as exc_info:
            await get_query_cache_endpoint(
                trace_id="tr_secret",
                tool_name=None,
                db=AsyncMock(),
                _current_user=_user(),
            )

    assert exc_info.value.error_code == "AI_QUERY_CACHE_NOT_FOUND"


async def test_authorized_owner_receives_safe_navigation_filters() -> None:
    entry = _entry()
    with (
        patch(
            "app.modules.ai.api.query_cache.get_query_cache",
            AsyncMock(return_value=entry),
        ),
        patch(
            "app.modules.ai.api.query_cache.result_projection_service.authorize_result_projection",
            AsyncMock(return_value=True),
        ),
    ):
        result = await get_query_cache_endpoint(
            trace_id="tr_visible",
            tool_name=None,
            db=AsyncMock(),
            _current_user=_user(),
        )

    assert result.data.tool_name == "user.list"
    assert result.data.filters == {"status": "1"}
