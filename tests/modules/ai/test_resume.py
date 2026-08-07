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
    tenant_id: int = 0,
    wake_action: str | None = None,
    expires_at: str = "2099-01-01T00:00:00Z",
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


@pytest.fixture(autouse=True)
def _mock_pending_delete():
    with patch(
        "app.modules.ai.api.resume.hitl_manager.delete_pending",
        AsyncMock(),
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
        跨请求同进程工作。多 worker 部署需 redis_pubsub 由 spec §8.4 / 部署文档约束。
        """
        from app.core.exceptions import NotFoundException

        with patch("app.modules.ai.api.resume.settings.AI_HITL_MODE", "memory"):
            # 端点不再因 memory 模式立即抛 410——会继续走 Redis 查 pending。
            # 用 mock 让 get_pending 返 None，验证端点往下走到 AI_RESUME_NOT_FOUND
            # 而不是 AI_RESUME_DISABLED。
            with patch(
                "app.modules.ai.api.resume.hitl_manager.get_pending",
                return_value=None,
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


# ============ 成功路径 ============


class TestResumeSuccessPath:
    """spec §3.1 + §4.3: 续传成功路径（emit resumed → hang → execute_tool → emit result）"""

    @pytest.fixture
    def _mock_deps_for_success(self, _resume_enabled, _redis_pubsub_mode):
        """成功路径所需的所有 mock：pending + ttl + 锁 + hang APPROVED + execute_tool"""
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.hitl.constants import ConfirmAction

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
                "app.modules.ai.api.resume.resume_tool_execution",
                AsyncMock(
                    return_value=(ToolResult.success(data={"affected_count": 1}), 150)
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

    async def test_rejected_path_emits_failure_result(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.hitl.constants import ConfirmAction

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


class TestOwnerLockRelease:
    """spec §2.3: owner 锁在 finally 块释放（Lua 脚本 token 校验）"""

    async def test_lock_released_after_success(
        self, _resume_enabled, _redis_pubsub_mode
    ) -> None:
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.hitl.constants import ConfirmAction

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
                "app.modules.ai.api.resume.resume_tool_execution",
                AsyncMock(return_value=(ToolResult.success(data={}), 100)),
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
