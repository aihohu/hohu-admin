"""spec §3 v1.5+: /ai/chat/resume 端点单元测试

直接调端点函数，mock 掉 redis_client + hitl_manager + settings。
"""

# ruff: noqa: ARG001, PLC0415

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.api.resume import resume_chat


def _make_pending(
    user_id: int = 100,
    wake_action: str | None = None,
    expires_at: str = "2099-01-01T00:00:00Z",
) -> PendingPayload:
    return PendingPayload(
        user_id=user_id,
        conversation_id=1,
        tool_call_id="tc_test",
        trace_id="tr_test",
        tool_name="user.update",
        args={"user_id": 42},
        dry_run_result=None,
        expires_at=expires_at,
        wake_action=wake_action,
    )


def _make_user(user_id: int = 100):
    return SimpleNamespace(user_id=user_id, user_name="alice")


def _make_request(last_event_id: str | None = None):
    """构造 FastAPI Request mock（headers.get('last-event-id')）"""
    req = MagicMock()
    req.headers = {"last-event-id": last_event_id} if last_event_id else {}
    return req


@pytest.fixture
def _redis_pubsub_mode():
    with patch("app.modules.ai.api.resume.settings.AI_HITL_MODE", "redis_pubsub"):
        yield


@pytest.fixture
def _resume_enabled():
    with patch("app.modules.ai.api.resume.settings.AI_SSE_RESUME_ENABLED", True):
        yield


# ============ 410 AI_RESUME_DISABLED ============


class TestResumeDisabled:
    async def test_feature_disabled_returns_410(self) -> None:
        with patch("app.modules.ai.api.resume.settings.AI_SSE_RESUME_ENABLED", False):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_DISABLED"
        assert exc_info.value.code == 410

    async def test_memory_mode_returns_410(self, _resume_enabled) -> None:
        with patch("app.modules.ai.api.resume.settings.AI_HITL_MODE", "memory"):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_DISABLED"
        assert exc_info.value.code == 410


# ============ 400 AI_RESUME_MISSING_ID ============


class TestResumeMissingId:
    async def test_no_header_no_query_returns_400(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with pytest.raises(BusinessRuleException) as exc_info:
            await resume_chat(
                request=_make_request(last_event_id=None),
                confirmation_id_query=None,
                db=MagicMock(),
                current_user=_make_user(),
            )
        assert exc_info.value.error_code == "AI_RESUME_MISSING_ID"
        assert exc_info.value.code == 400


# ============ 404 AI_RESUME_NOT_FOUND ============


class TestResumeNotFound:
    async def test_pending_missing_returns_404(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=None),
        ):
            with pytest.raises(NotFoundException) as exc_info:
                await resume_chat(
                    request=_make_request("cid_unknown"),
                    confirmation_id_query="cid_unknown",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_NOT_FOUND"


# ============ 403 AI_RESUME_FORBIDDEN ============


class TestResumeForbidden:
    async def test_owner_mismatch_returns_403(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=_make_pending(user_id=100)),
        ):
            with pytest.raises(AuthorizationException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(user_id=999),  # 非 owner
                )
        assert exc_info.value.error_code == "AI_RESUME_FORBIDDEN"


# ============ 410 AI_RESUME_ALREADY_RESOLVED ============


class TestResumeAlreadyResolved:
    async def test_wake_action_set_returns_410(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=_make_pending(wake_action="approved")),
        ):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_ALREADY_RESOLVED"
        assert exc_info.value.code == 410


# ============ 422 AI_RESUME_TTL_TOO_SHORT ============


class TestResumeTtlTooShort:
    async def test_ttl_below_60s_returns_422(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.ttl",
                AsyncMock(return_value=30),
            ),
        ):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_TTL_TOO_SHORT"
        assert exc_info.value.code == 422


# ============ 409 AI_RESUME_IN_PROGRESS ============


class TestResumeInProgress:
    async def test_lock_held_returns_409(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.ttl",
                AsyncMock(return_value=120),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=None),  # SETNX 失败
            ),
        ):
            with pytest.raises(BusinessRuleException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        assert exc_info.value.error_code == "AI_RESUME_IN_PROGRESS"
        assert exc_info.value.code == 409


# ============ Last-Event-ID 头优先级 ============


class TestLastEventIdHeaderPriority:
    async def test_header_preferred_over_query(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        """同时设头（cid_from_header）和 query param（cid_from_query）→ 用头"""
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=None),
        ) as mock_get:
            with pytest.raises(NotFoundException):
                await resume_chat(
                    request=_make_request(last_event_id="cid_from_header"),
                    confirmation_id_query="cid_from_query",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        args = mock_get.await_args.args
        assert args[1] == "cid_from_header"

    async def test_query_param_fallback(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with patch(
            "app.modules.ai.api.resume.hitl_manager.get_pending",
            AsyncMock(return_value=None),
        ) as mock_get:
            with pytest.raises(NotFoundException):
                await resume_chat(
                    request=_make_request(last_event_id=None),
                    confirmation_id_query="cid_from_query",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        args = mock_get.await_args.args
        assert args[1] == "cid_from_query"
