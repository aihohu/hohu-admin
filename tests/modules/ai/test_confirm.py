"""HITL /ai/confirm 端点单元测试（spec §8.3，含 2026-07-10 修订 S-13 / S-14）

直接调端点函数，mock 掉 deps（不用 TestClient，避免 FastAPI lifespan + DB
+ auth 中间件的全套启动开销）。

覆盖：
  - 正常 approve/reject 路径（status=queued）
  - pending 不存在 / 已过期 → 404
  - owner mismatch → 403 NOT_CONFIRMATION_OWNER
  - **修订 S-13**：用户被自动禁用 → 403 AI_USER_DISABLED
  - **修订 S-14**：wake 失败 → 200 + status=stream_gone + mark_expired_if_pending
  - **修订 S-14**：防双击 race（wake 第二次返回 False 触发 stream_gone 路径）
"""

# ruff: noqa: ARG001, PLC0415

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AuthorizationException, NotFoundException
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.api.confirm import confirm_tool
from app.modules.ai.schemas.confirm import ConfirmRequest


def _make_pending(
    user_id: int = 100,
    tenant_id: int = 0,
    *,
    source_user_message_id: int | None = None,
    guard_owner_token: str | None = None,
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
        expires_at="2026-07-10T15:00:00Z",
        source_user_message_id=source_user_message_id,
        guard_owner_token=guard_owner_token,
    )


def _make_user(user_id: int = 100, user_name: str = "alice"):
    """构造最小 User mock（只需 user_id / user_name 字段）"""
    from types import SimpleNamespace

    return SimpleNamespace(user_id=user_id, user_name=user_name)


@pytest.fixture(autouse=True)
def _mock_external():
    """统一 mock redis_client + AsyncSessionLocal，避免污染真实 Redis / DB"""
    with (
        patch("app.modules.ai.api.confirm.redis_client") as mock_redis,
        patch("app.modules.ai.api.confirm.AsyncSessionLocal") as mock_session_local,
    ):
        # mock_redis 默认所有方法返回 False / None / 空值
        mock_redis.exists = AsyncMock(return_value=False)

        # AsyncSessionLocal() async with → yield mock session
        mock_session_ctx = MagicMock()
        mock_session_local.return_value.__aenter__ = AsyncMock(
            return_value=mock_session_ctx
        )
        mock_session_local.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_session_ctx.begin.return_value.__aenter__ = AsyncMock(
            return_value=mock_session_ctx
        )
        mock_session_ctx.begin.return_value.__aexit__ = AsyncMock(return_value=None)

        yield {"redis": mock_redis, "session_local": mock_session_local}


# ============ 正常路径 ============


class TestConfirmSuccess:
    async def test_approve_wakes_stream_returns_queued(self) -> None:
        """approve + wake 成功 → status=queued"""
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=None),
            ),
        ):
            req = ConfirmRequest(confirmationId="cid_test_123", action="approved")
            db_mock = MagicMock()
            db_mock.commit = AsyncMock()

            result = await confirm_tool(req, db=db_mock, current_user=_make_user(100))

        assert result.code == 200
        assert result.data.tool_call_id == "tc_test"
        assert result.data.status == "queued"

    async def test_reject_wakes_stream_returns_queued(self) -> None:
        """reject + wake 成功 → status=queued（reject 也是 wake 成功）"""
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=None),
            ),
        ):
            req = ConfirmRequest(confirmationId="cid_test_123", action="rejected")
            db_mock = MagicMock()
            db_mock.commit = AsyncMock()

            result = await confirm_tool(req, db=db_mock, current_user=_make_user(100))

        assert result.data.status == "queued"


# ============ pending 不存在 / 已过期 ============


class TestPendingMissing:
    async def test_pending_not_found_raises_404(self) -> None:
        with patch(
            "app.modules.ai.api.confirm.hitl_manager.get_pending",
            AsyncMock(return_value=None),
        ):
            req = ConfirmRequest(confirmationId="cid_unknown_0123", action="approved")
            with pytest.raises(NotFoundException) as exc_info:
                await confirm_tool(req, db=MagicMock(), current_user=_make_user(100))
            assert exc_info.value.error_code == "CONFIRMATION_EXPIRED_OR_NOT_FOUND"


# ============ owner 校验 ============


class TestOwnerMismatch:
    async def test_owner_mismatch_raises_403(self) -> None:
        """非 owner（pending.user_id != current_user.user_id）→ 403"""
        with patch(
            "app.modules.ai.api.confirm.hitl_manager.get_pending",
            AsyncMock(return_value=_make_pending(user_id=100)),  # owner=100
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approved")
            with pytest.raises(AuthorizationException) as exc_info:
                # attacker user_id=999
                await confirm_tool(req, db=MagicMock(), current_user=_make_user(999))
            assert exc_info.value.error_code == "NOT_CONFIRMATION_OWNER"

    async def test_tenant_mismatch_is_rejected_before_wake(self) -> None:
        """同一 user_id 也不能确认其它可信 tenant 创建的 pending。"""
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending(user_id=100, tenant_id=999)),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(),
            ) as wake,
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approved")
            with pytest.raises(AuthorizationException) as exc_info:
                await confirm_tool(req, db=MagicMock(), current_user=_make_user(100))

        assert exc_info.value.error_code == "NOT_CONFIRMATION_OWNER"
        wake.assert_not_awaited()


# ============ 修订 S-13：用户被自动禁用 ============


class TestUserDisabled:
    """修订 S-13：HITL 期间用户被自动禁用 → 403 AI_USER_DISABLED"""

    async def test_disabled_user_blocked(self) -> None:
        """check_user_disabled=True → 抛 AI_USER_DISABLED"""
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending(user_id=100)),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=True),  # 用户已被禁用
            ),
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approved")
            with pytest.raises(AuthorizationException) as exc_info:
                await confirm_tool(req, db=MagicMock(), current_user=_make_user(100))
            assert exc_info.value.error_code == "AI_USER_DISABLED"

    async def test_disabled_check_runs_after_owner_check(self) -> None:
        """S-13：owner 校验先于 disabled 校验（owner 不匹配时不应触发 disabled 查询）"""
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending(user_id=100)),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=True),
            ) as mock_disabled,
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approved")
            with pytest.raises(AuthorizationException) as exc_info:
                await confirm_tool(
                    req,
                    db=MagicMock(),
                    current_user=_make_user(999),  # 非 owner
                )
            # owner mismatch 优先
            assert exc_info.value.error_code == "NOT_CONFIRMATION_OWNER"
            # disabled 检查不应被调用
            mock_disabled.assert_not_called()


# ============ 修订 S-14：wake 失败 → stream_gone ============


class TestWakeStreamGone:
    """修订 S-14：wake 返回 False（stream_gone）时返回特殊 status

    场景：服务重启 / 单 worker 切换 / SSE 已被中断 / 双击 race。
    端点必须：
      1. mark_expired_if_pending（仅 pending_confirmation 状态迁移）
      2. 返回 status="stream_gone"（前端停止轮询）
    """

    async def test_wake_stream_gone_returns_stream_gone_status(self) -> None:
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),  # stream_gone
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_expired_if_pending",
                AsyncMock(return_value=None),
            ) as mock_mark_expired,
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approved")
            result = await confirm_tool(
                req, db=MagicMock(), current_user=_make_user(100)
            )

        # 端点不抛，返回特殊 status
        assert result.code == 200
        assert result.data.status == "stream_gone"
        assert result.data.tool_call_id == "tc_test"
        # log 为 None 时 mark_expired 不调用
        mock_mark_expired.assert_not_called()

    async def test_wake_stream_gone_marks_expired_when_log_exists(self) -> None:
        """log 存在时 wake 失败必须尝试 mark_expired_if_pending（兜底审计）"""
        from types import SimpleNamespace

        fake_log = SimpleNamespace(log_id=12345)
        pending = _make_pending(
            source_user_message_id=987,
            guard_owner_token="owner-token",
        )

        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=pending),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=fake_log),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_approved",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_expired_if_pending",
                AsyncMock(return_value=fake_log),
            ) as mock_mark_expired,
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_pending_turn",
                AsyncMock(),
            ) as finalize,
            patch(
                "app.modules.ai.api.confirm.chat_run_guard.release",
                AsyncMock(),
            ) as release,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending",
                AsyncMock(),
            ) as delete_pending,
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approved")
            db_mock = MagicMock()
            db_mock.commit = AsyncMock()

            result = await confirm_tool(req, db=db_mock, current_user=_make_user(100))

        assert result.data.status == "stream_gone"
        # mark_expired_if_pending 应被调用一次（在独立 session 里）
        mock_mark_expired.assert_awaited_once()
        finalize.assert_awaited_once()
        release.assert_awaited_once()
        assert release.await_args.kwargs == {
            "conversation_id": 1,
            "owner_token": "owner-token",
        }
        delete_pending.assert_awaited_once()

    async def test_second_approve_does_not_finalize_or_release_running_turn(
        self,
    ) -> None:
        """A losing duplicate confirm must not terminate the already-running stream."""
        from types import SimpleNamespace

        fake_log = SimpleNamespace(log_id=12345)
        pending = _make_pending(
            source_user_message_id=987,
            guard_owner_token="owner-token",
        )

        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=pending),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=fake_log),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_approved",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_expired_if_pending",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_pending_turn",
                AsyncMock(),
            ) as finalize,
            patch(
                "app.modules.ai.api.confirm.chat_run_guard.release",
                AsyncMock(),
            ) as release,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending",
                AsyncMock(),
            ) as delete_pending,
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approved")
            db_mock = MagicMock()
            db_mock.commit = AsyncMock()

            result = await confirm_tool(req, db=db_mock, current_user=_make_user(100))

        assert result.data.status == "stream_gone"
        finalize.assert_not_awaited()
        release.assert_not_awaited()
        delete_pending.assert_not_awaited()

    async def test_wake_stream_gone_mark_expired_failure_does_not_break_response(
        self, _mock_external: Any
    ) -> None:
        """mark_expired_if_pending 抛异常时不阻断端点响应（审计 gap 走告警）"""
        from types import SimpleNamespace

        fake_log = SimpleNamespace(log_id=12345)

        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending()),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=fake_log),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_approved",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_expired_if_pending",
                AsyncMock(side_effect=Exception("DB down")),
            ),
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approved")
            db_mock = MagicMock()
            db_mock.commit = AsyncMock()
            result = await confirm_tool(req, db=db_mock, current_user=_make_user(100))

        # 仍返回 stream_gone（mark_expired 失败被吞掉）
        assert result.data.status == "stream_gone"
