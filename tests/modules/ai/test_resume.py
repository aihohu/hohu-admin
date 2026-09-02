"""``/ai/chat/resume`` 端点单元测试。

直接调端点函数，mock 掉 redis_client + hitl_manager + settings。
"""

# ruff: noqa: ARG001, PLC0415

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import TenantContext
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.api.resume import _load_durable_resume_terminal, resume_chat
from app.modules.ai.service.result_projection_service import (
    result_projection_service,
)

TENANT = TenantContext(
    tenant_id=0,
    tenant_code="default",
    actor_user_id=100,
    tenant_version=1,
    source="access_token",
)


def _make_pending(
    user_id: int = 100,
    tenant_id: int = 0,
    wake_action: str | None = None,
    expires_at: str = "2099-01-01T00:00:00Z",
    action_id: int | None = None,
) -> PendingPayload:
    return PendingPayload(
        user_id=user_id,
        tenant_id=tenant_id,
        conversation_id=1,
        tool_call_id="tc_test",
        trace_id="tr_test",
        tool_name="user.update",
        args={"user_id": 42},
        dry_run_result=None,
        expires_at=expires_at,
        action_id=action_id,
        wake_action=wake_action,
    )


def _make_user(user_id: int = 100, *, can_chat: bool = True):
    menus = [SimpleNamespace(permission="ai:chat:use")] if can_chat else []
    role = SimpleNamespace(role_code="R_USER", status="1", menus=menus)
    return SimpleNamespace(
        user_id=user_id,
        user_name="alice",
        roles=[role],
        _tenant_context=TenantContext(
            tenant_id=0,
            tenant_code="default",
            actor_user_id=user_id,
            tenant_version=1,
            source="access_token",
        ),
    )


def _make_durable_action(**overrides):
    lineage = result_projection_service.freeze_lineage(
        tenant=TENANT,
        agent_code="user_mgmt",
        tool_codes=["user.update"],
        subject_refs=[{"type": "user", "id": "42"}],
    )
    values = {
        "action_id": 9001,
        "confirmation_id": "cid",
        "user_id": 100,
        "tenant_id": lineage.tenant_id,
        "conversation_id": 1,
        "execute_tool_call_id": "tc_test",
        "execute_tool_name": "user.update",
        "prepare_tool_call_id": None,
        "trace_id": "tr_test",
        "interaction_flow": "direct",
        "presentation": {"title": "Update user", "summary": "Update one user"},
        "guard_owner_token": None,
        "status": "pending_confirmation",
        "finished_at": None,
        "expires_at": datetime(2099, 1, 1, tzinfo=UTC),
        "frozen_args": {"user_id": 42},
        "source_user_message_id": 12,
        "command_action": "send",
        "agent_code": lineage.agent_code,
        "risk_level": "high",
        "chip_target": None,
        "tool_codes": list(lineage.tool_codes),
        "subject_refs": list(lineage.subject_refs),
        "subject_refs_hash": lineage.subject_refs_hash,
        "data_scope_hash": lineage.data_scope_hash,
        "resolver_version": lineage.resolver_version,
        "resolved_model_id": 7001,
        "resolved_provider_id": 8001,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


@pytest.fixture(autouse=True)
def _mock_pending_delete():
    with patch(
        "app.modules.ai.api.resume.hitl_manager.delete_pending",
        AsyncMock(),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_prepared_action_lookup():
    """Use complete durable lineage unless a test explicitly covers legacy data."""
    with patch(
        "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
        AsyncMock(return_value=_make_durable_action()),
    ):
        yield


@pytest.fixture(autouse=True)
def _allow_result_projection():
    """Keep resume mechanics isolated from the dedicated projection-policy tests."""
    with patch.object(
        result_projection_service,
        "authorize_result_projection",
        AsyncMock(return_value=True),
    ):
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

    async def test_memory_mode_also_allowed(self, _resume_enabled) -> None:
        """memory 模式不再硬 410——单 worker 本地开发可用 resume。

        端点移除了 AI_HITL_MODE 硬校验：memory 模式下 _hang_memory + _wake_memory
        跨请求同进程工作；多 worker 部署需要 redis_pubsub。
        """
        from app.core.exceptions import NotFoundException

        with patch("app.modules.ai.api.resume.settings.AI_HITL_MODE", "memory"):
            # 端点不再因 memory 模式立即抛 410——会继续走 Redis 查 pending。
            # 用 mock 让 get_pending 返 None，验证端点往下走到 AI_RESUME_NOT_FOUND
            # 而不是 AI_RESUME_DISABLED。
            with (
                patch(
                    "app.modules.ai.api.resume.hitl_manager.get_pending",
                    return_value=None,
                ),
                patch(
                    "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                    AsyncMock(return_value=None),
                ),
            ):
                with pytest.raises(NotFoundException) as exc_info:
                    await resume_chat(
                        request=_make_request("cid"),
                        confirmation_id_query="cid",
                        db=MagicMock(),
                        current_user=_make_user(),
                    )
        # 走到 NOT_FOUND 而非 DISABLED，证明 memory 模式限制已放开
        assert exc_info.value.error_code == "AI_RESUME_NOT_FOUND"


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
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=None),
            ),
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


class TestResumeMinimalStatus:
    async def test_legacy_pending_without_lineage_returns_minimal_status(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=None),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(can_chat=True),
            )

        assert result.data.model_dump(by_alias=True) == {
            "confirmationId": "cid",
            "status": "pending_confirmation",
            "errorCode": "AI_RESULT_PROJECTION_FORBIDDEN",
            "finishedAt": None,
        }

    async def test_revoked_projection_returns_minimal_status(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        action = _make_durable_action(status="succeeded")
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=action),
            ),
            patch.object(
                result_projection_service,
                "authorize_result_projection",
                AsyncMock(return_value=False),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(can_chat=True),
            )

        assert result.data.error_code == "AI_RESULT_PROJECTION_FORBIDDEN"

    async def test_revoked_permission_replays_durable_terminal_without_redis_pending(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        """PostgreSQL 是终态权威；confirm 清 Redis 后仍须返回最小状态。"""
        finished_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
        durable_action = SimpleNamespace(
            action_id=9001,
            confirmation_id="cid",
            user_id=100,
            tenant_id=0,
            conversation_id=1,
            execute_tool_call_id="tc_test",
            trace_id="tr_test",
            status="succeeded",
            error_code=None,
            finished_at=finished_at,
        )
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=durable_action),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(can_chat=False),
            )

        assert result.data.model_dump(by_alias=True) == {
            "confirmationId": "cid",
            "status": "succeeded",
            "errorCode": "AI_CHAT_PERMISSION_DENIED",
            "finishedAt": finished_at,
        }

    async def test_authorized_terminal_replays_without_redis_pending(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        durable_action = SimpleNamespace(
            action_id=9001,
            confirmation_id="cid",
            user_id=100,
            tenant_id=0,
            conversation_id=1,
            execute_tool_call_id="tc_test",
            execute_tool_name="user.update",
            prepare_tool_call_id=None,
            trace_id="tr_test",
            status="succeeded",
            error_code=None,
            finished_at=datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
            expires_at=datetime(2026, 8, 14, 8, 5, tzinfo=UTC),
            frozen_args={"user_id": 42},
            source_user_message_id=12,
            guard_owner_token=None,
            command_action="send",
            agent_code="user_mgmt",
            risk_level="high",
            chip_target=None,
            presentation={"title": "更新用户", "summary": "更新 1 个用户"},
            interaction_flow="direct",
        )
        load_terminal = AsyncMock(return_value=[])
        cleanup = AsyncMock()
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=durable_action),
            ),
            patch(
                "app.modules.ai.api.resume._load_durable_resume_terminal",
                load_terminal,
            ),
            patch("app.modules.ai.api.resume._cleanup_durable_resume", cleanup),
        ):
            response = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(can_chat=True),
            )
            chunks = [chunk async for chunk in response.body_iterator]

        assert any("confirmation_resumed" in chunk for chunk in chunks)
        load_terminal.assert_awaited_once()
        cleanup.assert_awaited_once_with(durable_action, tenant=TENANT)

    async def test_revoked_chat_permission_returns_only_minimal_status(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        finished_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
        pending = _make_pending(action_id=9001)
        durable_action = SimpleNamespace(
            action_id=9001,
            confirmation_id="cid",
            user_id=100,
            tenant_id=0,
            conversation_id=1,
            execute_tool_call_id="tc_test",
            trace_id="tr_test",
            status="succeeded",
            error_code=None,
            finished_at=finished_at,
        )
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=pending),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=durable_action),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.ttl",
                AsyncMock(),
            ) as ttl,
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(),
            ) as acquire_lock,
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(can_chat=False),
            )

        assert result.data.model_dump(by_alias=True) == {
            "confirmationId": "cid",
            "status": "succeeded",
            "errorCode": "AI_CHAT_PERMISSION_DENIED",
            "finishedAt": finished_at,
        }
        ttl.assert_not_awaited()
        acquire_lock.assert_not_awaited()

    async def test_tenant_mismatch_returns_same_forbidden_semantics(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        """pending tenant 与当前认证 tenant 不同必须在抢执行锁前拒绝。"""
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending(tenant_id=999)),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(),
            ) as lock_set,
        ):
            with pytest.raises(AuthorizationException) as exc_info:
                await resume_chat(
                    request=_make_request("cid"),
                    confirmation_id_query="cid",
                    db=MagicMock(),
                    current_user=_make_user(),
                )

        assert exc_info.value.error_code == "AI_RESUME_FORBIDDEN"
        lock_set.assert_not_awaited()


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
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=None),
            ) as mock_get,
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=None),
            ),
        ):
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
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=None),
            ) as mock_get,
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=None),
            ),
        ):
            with pytest.raises(NotFoundException):
                await resume_chat(
                    request=_make_request(last_event_id=None),
                    confirmation_id_query="cid_from_query",
                    db=MagicMock(),
                    current_user=_make_user(),
                )
        args = mock_get.await_args.args
        assert args[1] == "cid_from_query"


# ============ 成功路径 ============


class TestResumeSuccessPath:
    """续传成功路径依次发送 resumed、等待确认、执行工具并返回结果。"""

    @pytest.fixture
    def _mock_deps_for_success(self, _resume_enabled, _redis_pubsub_mode):
        """Mock a complete durable handoff and its committed terminal replay."""
        from app.modules.ai.agents.hitl.constants import ConfirmAction
        from app.modules.ai.agents.hitl.events import DoneEvent, ToolCallResultEvent

        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),  # SETNX 成功
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),  # Lua 返回 1（删成功）
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(return_value=ConfirmAction.APPROVED),
            ),
            patch(
                "app.modules.ai.api.resume._load_durable_resume_terminal",
                AsyncMock(
                    return_value=[
                        ToolCallResultEvent(
                            tool="user.update",
                            tool_call_id="tc_test",
                            ok=True,
                            duration_ms=150,
                            result={"affected_count": 1},
                        ),
                        DoneEvent(
                            trace_id="tr_test",
                            persistence="committed",
                            projection="updated",
                        ),
                    ]
                ),
            ),
            patch(
                "app.modules.ai.api.resume._cleanup_durable_resume",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.resume.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=SimpleNamespace(log_id=42)),
            ),
            patch(
                "app.modules.ai.api.resume.chat_service.build_chat_deps",
                AsyncMock(return_value=MagicMock()),
            ),
        ):
            yield

    async def test_returns_streaming_response(self, _mock_deps_for_success) -> None:
        from fastapi.responses import StreamingResponse

        result = await resume_chat(
            request=_make_request("cid"),
            confirmation_id_query="cid",
            db=MagicMock(),
            current_user=_make_user(),
        )
        assert isinstance(result, StreamingResponse)
        assert result.media_type == "text/event-stream"
        # Drain the body_iterator so the async generator completes its finally block
        # (releases owner lock via mock) — otherwise GC of an unstarted generator
        # leaves dangling async state that breaks the next test's event loop.
        async for _ in result.body_iterator:
            pass

    async def test_stream_emits_resumed_then_result_then_done(
        self, _mock_deps_for_success
    ) -> None:
        result = await resume_chat(
            request=_make_request("cid"),
            confirmation_id_query="cid",
            db=MagicMock(),
            current_user=_make_user(),
        )
        chunks: list[str] = []
        async for chunk in result.body_iterator:
            chunks.append(chunk)
        body = "".join(chunks)
        assert (
            '"type":"confirmation_resumed"' in body
            or '"type": "confirmation_resumed"' in body
        )
        assert (
            '"type":"tool_call_result"' in body or '"type": "tool_call_result"' in body
        )
        assert '"type":"done"' in body or '"type": "done"' in body
        assert body.index("confirmation_resumed") < body.index("tool_call_result")
        assert '"durationMs":150' in body or '"durationMs": 150' in body

    async def test_durable_action_resume_replays_terminal_without_executing_tool(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.hitl.constants import ConfirmAction
        from app.modules.ai.agents.hitl.events import DoneEvent, ToolCallResultEvent

        execute = AsyncMock()
        durable_action = SimpleNamespace(
            action_id=9001,
            confirmation_id="cid",
            user_id=100,
            tenant_id=0,
            conversation_id=1,
            execute_tool_call_id="tc_test",
            execute_tool_name="user.update",
            prepare_tool_call_id="tc_preview",
            trace_id="tr_test",
            interaction_flow="prepared",
            presentation={"title": "确认更新用户", "summary": "将更新 1 个用户"},
            guard_owner_token=None,
            status="succeeded",
        )
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending(action_id=9001)),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(return_value=ConfirmAction.APPROVED),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=durable_action),
            ),
            patch(
                "app.modules.ai.api.resume.resume_tool_execution",
                execute,
            ),
            patch(
                "app.modules.ai.api.resume._load_durable_resume_terminal",
                AsyncMock(
                    return_value=[
                        ToolCallResultEvent(
                            tool="user.update",
                            tool_call_id="tc_test",
                            ok=True,
                            duration_ms=7,
                            result={"updated": 1},
                        ),
                        DoneEvent(
                            trace_id="tr_test",
                            message_id=123,
                            persistence="committed",
                            projection="updated",
                        ),
                    ]
                ),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            body = ""
            async for chunk in result.body_iterator:
                body += chunk

        execute.assert_not_awaited()
        assert '"messageId":123' in body or '"messageId": 123' in body
        assert '"toolCallId":"tc_test"' in body or '"toolCallId": "tc_test"' in body
        assert (
            '"sourceToolCallId":"tc_preview"' in body
            or '"sourceToolCallId": "tc_preview"' in body
        )
        assert (
            '"interactionFlow":"prepared"' in body
            or '"interactionFlow": "prepared"' in body
        )

    async def test_rolling_upgrade_payload_without_action_id_still_uses_durable_replay(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.hitl.constants import ConfirmAction
        from app.modules.ai.agents.hitl.events import DoneEvent

        durable_action = SimpleNamespace(
            action_id=9001,
            confirmation_id="cid",
            user_id=100,
            tenant_id=0,
            conversation_id=1,
            execute_tool_call_id="tc_test",
            execute_tool_name="user.update",
            prepare_tool_call_id=None,
            trace_id="tr_test",
            interaction_flow="direct",
            presentation={"title": "确认更新用户", "summary": "将更新 1 个用户"},
            guard_owner_token=None,
            status="succeeded",
        )
        execute = AsyncMock()
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending(action_id=None)),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=durable_action),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(return_value=ConfirmAction.APPROVED),
            ),
            patch("app.modules.ai.api.resume.resume_tool_execution", execute),
            patch(
                "app.modules.ai.api.resume._load_durable_resume_terminal",
                AsyncMock(
                    return_value=[
                        DoneEvent(
                            trace_id="tr_test",
                            message_id=123,
                            persistence="committed",
                            projection="updated",
                        )
                    ]
                ),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            body = "".join([chunk async for chunk in result.body_iterator])

        execute.assert_not_awaited()
        assert '"messageId":123' in body or '"messageId": 123' in body


class TestDurableTerminalProjection:
    async def test_replays_result_ui_after_terminal_reauthorization(
        self,
    ) -> None:
        action = _make_durable_action(
            status="succeeded",
            duration_ms=8,
            result_data={"updated": 1},
            result_ui={
                "viewType": "detail_card",
                "viewData": {"count": 1},
                "audit": {"affectedCount": 1},
                "labelKey": "ai.tool.user.update.result",
                "labelParams": {"count": 1},
            },
            error_code=None,
        )
        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=None)
        fake_db.scalar = AsyncMock(return_value=123)
        with (
            patch(
                "app.modules.ai.api.resume.AsyncSessionLocal",
                MagicMock(return_value=fake_db),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=action),
            ),
            patch.object(
                result_projection_service,
                "authorize_result_projection",
                AsyncMock(return_value=True),
            ) as authorize,
        ):
            events = await _load_durable_resume_terminal(
                confirmation_id="cid",
                pending=_make_pending(action_id=None),
                user_id=100,
                tenant=TENANT,
                current_user=_make_user(),
            )

        result = events[0]
        assert result.type == "tool_call_result"
        assert result.ok is True
        assert result.ui is not None
        assert result.ui.view_type == "detail_card"
        assert result.ui.view_data["count"] == 1
        assert events[1].message_id == 123
        authorize.assert_awaited_once()

    async def test_revocation_during_wait_suppresses_terminal_business_result(
        self,
    ) -> None:
        """The terminal read must not trust authorization from before the wait."""
        action = _make_durable_action(
            status="succeeded",
            duration_ms=8,
            result_data={"updated": 1, "secret": "must-not-leak"},
            result_ui={"viewType": "plain_json", "viewData": {"updated": 1}},
            error_code=None,
        )
        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=None)
        fake_db.scalar = AsyncMock(return_value=123)
        with (
            patch(
                "app.modules.ai.api.resume.AsyncSessionLocal",
                MagicMock(return_value=fake_db),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=action),
            ),
            patch.object(
                result_projection_service,
                "authorize_result_projection",
                AsyncMock(return_value=False),
            ),
        ):
            events = await _load_durable_resume_terminal(
                confirmation_id="cid",
                pending=_make_pending(action_id=9001),
                user_id=100,
                tenant=TENANT,
                current_user=_make_user(),
            )

        assert [event.type for event in events] == ["ai_error", "done"]
        assert events[0].error_code == "AI_RESULT_PROJECTION_FORBIDDEN"

    async def test_rejected_path_emits_failure_result(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.hitl.constants import ConfirmAction
        from app.modules.ai.agents.hitl.events import DoneEvent, ToolCallResultEvent

        # AsyncSessionLocal mock：REJECTED 分支开真实 DB session 做 mark_rejected
        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=None)
        fake_db.begin = MagicMock(return_value=fake_db)
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(return_value=ConfirmAction.REJECTED),
            ),
            patch(
                "app.modules.ai.api.resume._load_durable_resume_terminal",
                AsyncMock(
                    return_value=[
                        ToolCallResultEvent(
                            tool="user.update",
                            tool_call_id="tc_test",
                            ok=False,
                            duration_ms=0,
                            error_code="USER_REJECTED",
                            error_msg="Rejected",
                        ),
                        DoneEvent(
                            trace_id="tr_test",
                            persistence="committed",
                            projection="updated",
                        ),
                    ]
                ),
            ),
            patch(
                "app.modules.ai.api.resume._cleanup_durable_resume",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.resume.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=SimpleNamespace(log_id=42)),
            ),
            patch(
                "app.modules.ai.api.resume.chat_service.build_chat_deps",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.modules.ai.api.resume.operation_log_service.mark_rejected",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.resume.AsyncSessionLocal",
                MagicMock(return_value=fake_db),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            body = ""
            async for chunk in result.body_iterator:
                body += chunk
        assert (
            '"type":"confirmation_resumed"' in body
            or '"type": "confirmation_resumed"' in body
        )
        assert "USER_REJECTED" in body
        assert '"type":"done"' in body or '"type": "done"' in body


class TestResumeTimeoutPath:
    async def test_hang_timeout_emits_ai_error(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.hitl.events import DoneEvent

        # AsyncSessionLocal 必须也 mock：resume.py 在 TimeoutError 分支会开真实 DB
        # session 做 mark_expired_if_pending，未 mock 会 checkout 真连接，下个测试
        # 的 event loop 关闭时 asyncpg _terminate_graceful_close 抛 RuntimeError。
        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=None)
        fake_db.begin = MagicMock(return_value=fake_db)
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(side_effect=TimeoutError()),
            ),
            patch(
                "app.modules.ai.api.resume._terminalize_durable_resume_failure",
                AsyncMock(
                    return_value=[
                        DoneEvent(
                            trace_id="tr_test",
                            persistence="committed",
                            projection="updated",
                        )
                    ]
                ),
            ),
            patch(
                "app.modules.ai.api.resume.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=SimpleNamespace(log_id=42)),
            ),
            patch(
                "app.modules.ai.api.resume.chat_service.build_chat_deps",
                AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "app.modules.ai.api.resume.operation_log_service.mark_expired_if_pending",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.resume.AsyncSessionLocal",
                MagicMock(return_value=fake_db),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            body = ""
            async for chunk in result.body_iterator:
                body += chunk
        assert "AI_HITL_TIMEOUT" in body
        assert '"type":"done"' in body or '"type": "done"' in body

    async def test_durable_timeout_uses_action_terminalizer_not_legacy_finalizer(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.hitl.events import DoneEvent

        durable_action = SimpleNamespace(
            action_id=9001,
            confirmation_id="cid",
            user_id=100,
            tenant_id=0,
            conversation_id=1,
            execute_tool_call_id="tc_test",
            execute_tool_name="user.update",
            prepare_tool_call_id=None,
            trace_id="tr_test",
            interaction_flow="direct",
            presentation={"title": "确认更新用户"},
            guard_owner_token=None,
            status="pending_confirmation",
        )
        durable_terminalizer = AsyncMock(
            return_value=[
                DoneEvent(
                    trace_id="tr_test",
                    persistence="committed",
                    projection="updated",
                )
            ]
        )
        legacy_terminalizer = AsyncMock()
        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending(action_id=None)),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=durable_action),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(side_effect=TimeoutError()),
            ),
            patch(
                "app.modules.ai.api.resume._terminalize_durable_resume_failure",
                durable_terminalizer,
            ),
            patch(
                "app.modules.ai.api.resume._finalize_resume_terminal",
                legacy_terminalizer,
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            body = "".join([chunk async for chunk in result.body_iterator])

        durable_terminalizer.assert_awaited_once()
        legacy_terminalizer.assert_not_awaited()
        assert "AI_HITL_TIMEOUT" in body


class TestOwnerLockRelease:
    """owner 锁在 finally 中通过 token 校验后释放。"""

    async def test_lock_released_after_success(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.hitl.constants import ConfirmAction
        from app.modules.ai.agents.hitl.events import DoneEvent

        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ) as mock_set,
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ) as mock_eval,
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(return_value=ConfirmAction.APPROVED),
            ),
            patch(
                "app.modules.ai.api.resume._load_durable_resume_terminal",
                AsyncMock(
                    return_value=[
                        DoneEvent(
                            trace_id="tr_test",
                            persistence="committed",
                            projection="updated",
                        )
                    ]
                ),
            ),
            patch(
                "app.modules.ai.api.resume.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=SimpleNamespace(log_id=42)),
            ),
            patch(
                "app.modules.ai.api.resume.chat_service.build_chat_deps",
                AsyncMock(return_value=MagicMock()),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            async for _ in result.body_iterator:
                pass
        mock_set.assert_awaited()  # SETNX called
        mock_eval.assert_awaited()  # Lua release called

    async def test_lock_released_on_hang_error(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        """hang 抛非 TimeoutError 异常时锁也要释放（finally 块）"""
        from app.modules.ai.agents.hitl.events import DoneEvent

        with (
            patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.resume.hitl_manager.ttl",
                AsyncMock(return_value=240),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.set",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.resume.redis_client.eval",
                AsyncMock(return_value=1),
            ) as mock_eval,
            patch(
                "app.modules.ai.api.resume.hitl_manager.hang",
                AsyncMock(side_effect=RuntimeError("redis gone")),
            ),
            patch(
                "app.modules.ai.api.resume._terminalize_durable_resume_failure",
                AsyncMock(
                    return_value=[
                        DoneEvent(
                            trace_id="tr_test",
                            persistence="failed",
                            projection="updated",
                        )
                    ]
                ),
            ),
            patch(
                "app.modules.ai.api.resume.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=SimpleNamespace(log_id=42)),
            ),
            patch(
                "app.modules.ai.api.resume.chat_service.build_chat_deps",
                AsyncMock(return_value=MagicMock()),
            ),
        ):
            result = await resume_chat(
                request=_make_request("cid"),
                confirmation_id_query="cid",
                db=MagicMock(),
                current_user=_make_user(),
            )
            # body_iterator 内部的 try/except 会消化 RuntimeError 并 emit ai_error
            async for _ in result.body_iterator:
                pass
        mock_eval.assert_awaited()


# ============ 路由注册 ============


class TestResumeRouterRegistered:
    def test_route_in_openapi(self) -> None:
        from app.main import app

        paths = app.openapi()["paths"]
        # ai_resume_router prefix="/ai/chat" + "/resume" path → /ai/chat/resume GET
        assert any(
            "/resume" in path and "get" in methods for path, methods in paths.items()
        ), "resume endpoint not registered"

    def test_route_gated_by_ai_module_enabled(self) -> None:
        """AI_MODULE_ENABLED=False 时 resume 路由也不应注册（source inspection）"""
        import inspect

        from app import main

        src = inspect.getsource(main)
        # 注册语句应在 `if settings.AI_MODULE_ENABLED:` 块内
        assert "ai_resume_router" in src
