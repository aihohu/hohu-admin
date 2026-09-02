"""HITL ``/ai/confirm`` 端点单元测试。

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

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import TenantContext
from app.modules.ai.agents.hitl.constants import ConfirmAction
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.api.confirm import _notify_prepared_terminal, confirm_tool
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


class TestPreparedTerminalCleanupOwnership:
    async def test_live_prepared_waiter_retains_guard_and_pending_for_stream(
        self,
    ) -> None:
        """成功 wake 后由在线 SSE 在最终 assistant commit 后清理 guard/pending。"""
        action = SimpleNamespace(
            action_id=901,
            confirmation_id="cid_live",
            conversation_id=902,
            guard_owner_token="owner-live",
        )
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=True),
            ) as wake,
            patch(
                "app.modules.ai.api.confirm.chat_run_guard.release", AsyncMock()
            ) as release,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending", AsyncMock()
            ) as delete_pending,
        ):
            await _notify_prepared_terminal(
                action,
                ConfirmAction.APPROVED,
                tenant=_make_user()._tenant_context,
            )

        wake.assert_awaited_once_with("cid_live", ConfirmAction.APPROVED, tenant=ANY)
        release.assert_not_awaited()
        delete_pending.assert_not_awaited()

    async def test_offline_prepared_waiter_cleans_guard_and_pending(self) -> None:
        """wake=False 表示没有在线 SSE，由 confirm handler 负责终态清理。"""
        action = SimpleNamespace(
            action_id=903,
            confirmation_id="cid_offline",
            conversation_id=904,
            guard_owner_token="owner-offline",
        )
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.chat_run_guard.release", AsyncMock()
            ) as release,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending", AsyncMock()
            ) as delete_pending,
        ):
            await _notify_prepared_terminal(
                action,
                ConfirmAction.REJECTED,
                tenant=_make_user()._tenant_context,
            )

        release.assert_awaited_once_with(
            ANY,
            conversation_id=904,
            owner_token="owner-offline",
            tenant=ANY,
        )
        delete_pending.assert_awaited_once_with(ANY, "cid_offline", tenant=ANY)


def _make_user(
    user_id: int = 100,
    user_name: str = "alice",
    *,
    can_chat: bool = True,
):
    """构造带显式 AI 入口权限的最小 User mock。"""
    menus = [SimpleNamespace(permission="ai:chat:use")] if can_chat else []
    role = SimpleNamespace(role_code="R_USER", status="1", menus=menus)
    return SimpleNamespace(
        user_id=user_id,
        user_name=user_name,
        roles=[role],
        _tenant_context=TenantContext(
            tenant_id=0,
            tenant_code="default",
            actor_user_id=user_id,
            tenant_version=1,
            source="access_token",
        ),
    )


def _make_prepared_action(
    *,
    status: str = "pending_confirmation",
    interaction_flow: str = "prepared",
    data_scope_hash: str | None = None,
):
    versions = {
        "pending_confirmation": 1,
        "approved": 2,
        "running": 3,
        "succeeded": 4,
        "failed": 4,
        "rejected": 2,
        "expired": 2,
    }
    return SimpleNamespace(
        action_id=9001,
        confirmation_id="cid_test_0123",
        user_id=100,
        tenant_id=0,
        conversation_id=1,
        source_user_message_id=987,
        execute_tool_call_id="tc_test",
        execute_tool_name="user.import_execute",
        interaction_flow=interaction_flow,
        requested_outcome=(
            "execute_if_approved" if interaction_flow == "prepared" else "direct"
        ),
        status=status,
        row_version=versions[status],
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        guard_owner_token=None,
        trace_id="tr_test",
        agent_code="user_mgmt",
        data_scope_hash=data_scope_hash,
        command_action="send",
        risk_level="high",
        chip_target=None,
        frozen_args={"preview_token": "server-secret"},
        args_hash="hash",
        presentation={
            "title": "Import users",
            "fields": [{"label": "new", "value": 2}],
            "warnings": [],
        },
        prepare_tool_call_id=("tc_preview" if interaction_flow == "prepared" else None),
        result_data={"successCount": 2} if status == "succeeded" else None,
        result_ui=None,
        duration_ms=2 if status == "succeeded" else None,
        error_code=None,
    )


def _make_prepared_context(action):  # noqa: ANN001
    return SimpleNamespace(
        action=action,
        conversation=SimpleNamespace(user_id=100),
        source_message=SimpleNamespace(conversation_id=1, role="user", is_active=True),
    )


def test_confirm_request_rejects_policy_override_fields() -> None:
    """confirm 只能选择批准或拒绝，不能提交新的工具参数。"""
    with pytest.raises(ValidationError):
        ConfirmRequest.model_validate(
            {
                "confirmationId": "cid_test_0123",
                "action": "approve",
                "onConflict": "overwrite",
            }
        )


@pytest.fixture(autouse=True)
def _mock_external():
    """统一 mock redis_client + AsyncSessionLocal，避免污染真实 Redis / DB"""
    with (
        patch("app.modules.ai.api.confirm.redis_client") as mock_redis,
        patch("app.modules.ai.api.confirm.AsyncSessionLocal") as mock_session_local,
        patch(
            "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
            AsyncMock(return_value=None),
        ),
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
            req = ConfirmRequest(confirmationId="cid_test_123", action="approve")
            db_mock = MagicMock()
            db_mock.commit = AsyncMock()

            result = await confirm_tool(req, db=db_mock, current_user=_make_user(100))

        assert result.code == 200
        assert result.data.tool_call_id == "tc_test"
        assert result.data.status == "queued"

    async def test_legacy_approve_without_permission_expires_and_cleans_pending(
        self,
    ) -> None:
        log = SimpleNamespace(log_id=77)
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=_make_pending(guard_owner_token="guard")),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=log),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_expired_if_pending",
                AsyncMock(return_value=log),
            ) as expire,
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_pending_turn",
                AsyncMock(),
            ) as finalize,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),
            ) as wake,
            patch(
                "app.modules.ai.api.confirm.chat_run_guard.release",
                AsyncMock(),
            ) as release,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending",
                AsyncMock(),
            ) as delete_pending,
        ):
            db = MagicMock()
            db.commit = AsyncMock()
            with pytest.raises(AuthorizationException) as exc_info:
                await confirm_tool(
                    ConfirmRequest(
                        confirmationId="cid_test_123",
                        action="approve",
                    ),
                    db=db,
                    current_user=_make_user(can_chat=False),
                )

        assert exc_info.value.error_code == "AI_CHAT_PERMISSION_DENIED"
        expire.assert_awaited_once_with(
            db,
            77,
            error_code="AI_CHAT_PERMISSION_DENIED",
            tenant=ANY,
        )
        finalize.assert_awaited_once()
        db.commit.assert_awaited_once()
        wake.assert_awaited_once_with(
            "cid_test_123", ConfirmAction.REJECTED, tenant=ANY
        )
        release.assert_awaited_once()
        delete_pending.assert_awaited_once()


class TestPreparedConfirmation:
    async def test_reject_bypasses_revoked_chat_permission_and_disabled_state(
        self,
    ) -> None:
        action = _make_prepared_action()
        terminal = _make_prepared_action(status="rejected")
        disabled_check = AsyncMock(return_value=True)
        with (
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                disabled_check,
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=action),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.lock_confirmation_context",
                AsyncMock(return_value=_make_prepared_context(action)),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.transition_status",
                AsyncMock(return_value=terminal),
            ),
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_prepared_action",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm._notify_prepared_terminal",
                AsyncMock(),
            ),
        ):
            db = MagicMock()
            db.commit = AsyncMock()
            result = await confirm_tool(
                ConfirmRequest(
                    confirmationId="cid_test_0123",
                    action="reject",
                ),
                db=db,
                current_user=_make_user(can_chat=False),
            )

        assert result.data.status == "rejected"
        disabled_check.assert_not_awaited()

    async def test_approve_without_chat_permission_terminalizes_before_execution(
        self,
    ) -> None:
        action = _make_prepared_action()
        terminal = _make_prepared_action(status="expired")
        log = SimpleNamespace(log_id=77)
        transition = AsyncMock(return_value=terminal)
        expire = AsyncMock(return_value=log)
        notify = AsyncMock()
        with (
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=action),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.lock_confirmation_context",
                AsyncMock(return_value=_make_prepared_context(action)),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=log),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_expired_if_pending",
                expire,
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.transition_status",
                transition,
            ),
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_prepared_action",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm._notify_prepared_terminal",
                notify,
            ),
            patch(
                "app.modules.ai.api.confirm.execute_approved_prepared_action",
                AsyncMock(),
            ) as execute,
        ):
            db = MagicMock()
            db.commit = AsyncMock()
            with pytest.raises(AuthorizationException) as exc_info:
                await confirm_tool(
                    ConfirmRequest(
                        confirmationId="cid_test_0123",
                        action="approve",
                    ),
                    db=db,
                    current_user=_make_user(can_chat=False),
                )

        assert exc_info.value.error_code == "AI_CHAT_PERMISSION_DENIED"
        assert transition.await_args.kwargs["target_status"].value == "expired"
        assert transition.await_args.kwargs["error_code"] == (
            "AI_CHAT_PERMISSION_DENIED"
        )
        expire.assert_awaited_once_with(
            db,
            log.log_id,
            error_code="AI_CHAT_PERMISSION_DENIED",
            tenant=ANY,
        )
        db.commit.assert_awaited_once()
        notify.assert_awaited_once()
        execute.assert_not_awaited()

    async def test_stale_snapshot_is_rejected_before_wake(self) -> None:
        action = _make_prepared_action()
        terminal = _make_prepared_action(status="expired")
        stale = BusinessRuleException(
            "preview snapshot changed",
            error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
        )
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
                "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=action),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.lock_confirmation_context",
                AsyncMock(return_value=_make_prepared_context(action)),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.validate_snapshot",
                AsyncMock(side_effect=stale),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),
            ) as wake,
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.transition_status",
                AsyncMock(return_value=terminal),
            ),
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_prepared_action",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending",
                AsyncMock(),
            ),
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approve")
            db = MagicMock()
            db.commit = AsyncMock()
            with pytest.raises(BusinessRuleException) as exc_info:
                await confirm_tool(
                    req,
                    db=db,
                    current_user=_make_user(100),
                )

        assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
        wake.assert_awaited_once()

    async def test_reject_does_not_require_business_snapshot_to_stay_fresh(
        self,
    ) -> None:
        action = _make_prepared_action()
        terminal = _make_prepared_action(status="rejected")
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
                "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=action),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.lock_confirmation_context",
                AsyncMock(return_value=_make_prepared_context(action)),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.validate_snapshot",
                AsyncMock(),
            ) as validate_snapshot,
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.transition_status",
                AsyncMock(return_value=terminal),
            ),
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_prepared_action",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending",
                AsyncMock(),
            ),
        ):
            db = MagicMock()
            db.commit = AsyncMock()
            result = await confirm_tool(
                ConfirmRequest(
                    confirmationId="cid_test_0123",
                    action="reject",
                ),
                db=db,
                current_user=_make_user(100),
            )

        assert result.data.status == "rejected"
        validate_snapshot.assert_not_awaited()

    @pytest.mark.parametrize("interaction_flow", ["prepared", "direct"])
    async def test_approve_executes_inline_without_redis_pending(
        self, interaction_flow: str
    ) -> None:
        from app.modules.ai.agents.gateway.result import ToolResult

        pending = _make_prepared_action(interaction_flow=interaction_flow)
        approved = _make_prepared_action(
            status="approved", interaction_flow=interaction_flow
        )
        running = _make_prepared_action(
            status="running", interaction_flow=interaction_flow
        )
        terminal = _make_prepared_action(
            status="succeeded", interaction_flow=interaction_flow
        )
        db = MagicMock()
        lifecycle: list[str] = []
        db.commit = AsyncMock(side_effect=lambda: lifecycle.append("commit"))
        db.rollback = AsyncMock()
        deps = SimpleNamespace(data_scope_hash="current-scope")
        validate_scope = MagicMock()
        with (
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
                AsyncMock(side_effect=[pending, running]),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.lock_confirmation_context",
                AsyncMock(return_value=_make_prepared_context(pending)),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.validate_snapshot",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.validate_data_scope_snapshot",
                validate_scope,
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.transition_status",
                AsyncMock(side_effect=[approved, running, terminal]),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.confirm.chat_service.build_chat_deps",
                AsyncMock(return_value=deps),
            ),
            patch("app.modules.ai.api.confirm.validate_prepared_execution"),
            patch(
                "app.modules.ai.api.confirm.execute_approved_prepared_action",
                AsyncMock(
                    side_effect=lambda *_args: (
                        lifecycle.append("execute")
                        or ToolResult.success({"successCount": 2})
                    )
                ),
            ) as execute,
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_prepared_action",
                AsyncMock(),
            ) as finalize,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(),
            ) as get_pending,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(
                    side_effect=lambda *_args, **_kwargs: (
                        lifecycle.append("wake") or False
                    )
                ),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
        ):
            result = await confirm_tool(
                ConfirmRequest(
                    confirmationId="cid_test_0123",
                    action="approve",
                ),
                db=db,
                current_user=_make_user(),
            )

        assert result.data.status == "succeeded"
        assert result.data.action_id == 9001
        assert db.commit.await_count == 2
        assert lifecycle == ["commit", "execute", "commit", "wake"]
        validate_scope.assert_called_once_with(
            pending,
            current_data_scope_hash="current-scope",
        )
        execute.assert_awaited_once_with(running, deps)
        finalize.assert_awaited_once()
        get_pending.assert_not_awaited()

    async def test_scope_bound_result_freezes_post_write_scope_hash(
        self,
        _mock_external,
    ) -> None:
        from app.modules.ai.agents.gateway.result import ResultProjection, ToolResult

        pending = _make_prepared_action(data_scope_hash="pre-write-scope")
        approved = _make_prepared_action(
            status="approved", data_scope_hash="pre-write-scope"
        )
        running = _make_prepared_action(
            status="running", data_scope_hash="pre-write-scope"
        )
        running.tool_codes = ["dept.create"]
        terminal = _make_prepared_action(
            status="succeeded", data_scope_hash="pre-write-scope"
        )
        transition = AsyncMock(side_effect=[approved, running, terminal])
        post_write_hash = AsyncMock(return_value="post-write-scope")
        deps = SimpleNamespace(data_scope_hash="pre-write-scope")
        current_user = _make_user()

        with (
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
                AsyncMock(side_effect=[pending, running]),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.lock_confirmation_context",
                AsyncMock(return_value=_make_prepared_context(pending)),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.validate_snapshot",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.validate_data_scope_snapshot"
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.transition_status",
                transition,
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.confirm.chat_service.build_chat_deps",
                AsyncMock(return_value=deps),
            ),
            patch("app.modules.ai.api.confirm.validate_prepared_execution"),
            patch(
                "app.modules.ai.api.confirm.execute_approved_prepared_action",
                AsyncMock(
                    return_value=ToolResult.success(
                        {"deptId": "42"},
                        projection=ResultProjection(
                            subject_refs=({"type": "dept", "id": "42"},),
                            scope_bound=True,
                        ),
                    )
                ),
            ),
            patch(
                "app.modules.ai.api.confirm.result_projection_service.compute_data_scope_hash",
                post_write_hash,
            ),
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_prepared_action",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
        ):
            db = MagicMock()
            db.commit = AsyncMock()
            db.rollback = AsyncMock()
            result = await confirm_tool(
                ConfirmRequest(
                    confirmationId="cid_test_0123",
                    action="approve",
                ),
                db=db,
                current_user=current_user,
            )

        post_write_hash.assert_awaited_once_with(db, current_user)
        assert db.commit.await_count == 2
        result_lineage = transition.await_args_list[2].kwargs["result_lineage"]
        assert result_lineage.data_scope_hash == "post-write-scope"
        assert result.data.status == "succeeded"

    async def test_unexpected_inline_execution_error_is_finalized_as_failed(
        self,
    ) -> None:
        pending = _make_prepared_action()
        approved = _make_prepared_action(status="approved")
        running = _make_prepared_action(status="running")
        terminal = _make_prepared_action(status="failed")
        terminal.error_code = "AI_INTERNAL_ERROR"
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        deps = SimpleNamespace()
        with (
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
                AsyncMock(side_effect=[pending, running]),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.lock_confirmation_context",
                AsyncMock(return_value=_make_prepared_context(pending)),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.validate_snapshot",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.transition_status",
                AsyncMock(side_effect=[approved, running, terminal]),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.modules.ai.api.confirm.chat_service.build_chat_deps",
                AsyncMock(return_value=deps),
            ),
            patch("app.modules.ai.api.confirm.validate_prepared_execution"),
            patch(
                "app.modules.ai.api.confirm.execute_approved_prepared_action",
                AsyncMock(
                    side_effect=RuntimeError("redis failure after tool rollback")
                ),
            ),
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_prepared_action",
                AsyncMock(),
            ) as finalize,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending",
                AsyncMock(),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
        ):
            result = await confirm_tool(
                ConfirmRequest(
                    confirmationId="cid_test_0123",
                    action="approve",
                ),
                db=db,
                current_user=_make_user(),
            )

        assert result.data.status == "failed"
        finalize.assert_awaited_once()
        assert finalize.await_args.kwargs["ok"] is False
        assert finalize.await_args.kwargs["error_code"] == "AI_INTERNAL_ERROR"

    async def test_duplicate_approve_returns_terminal_without_execution(self) -> None:
        terminal = _make_prepared_action(status="succeeded")
        with (
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
                AsyncMock(return_value=terminal),
            ),
            patch(
                "app.modules.ai.api.confirm.prepared_action_service.lock_confirmation_context",
                AsyncMock(return_value=_make_prepared_context(terminal)),
            ),
            patch(
                "app.modules.ai.api.confirm.execute_approved_prepared_action",
                AsyncMock(),
            ) as execute,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(),
            ) as get_pending,
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
        ):
            result = await confirm_tool(
                ConfirmRequest(
                    confirmationId="cid_test_0123",
                    action="approve",
                ),
                db=MagicMock(),
                current_user=_make_user(),
            )

        assert result.data.status == "succeeded"
        execute.assert_not_awaited()
        get_pending.assert_not_awaited()

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
            req = ConfirmRequest(confirmationId="cid_test_123", action="reject")
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
            req = ConfirmRequest(confirmationId="cid_unknown_0123", action="approve")
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
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approve")
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
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approve")
            with pytest.raises(AuthorizationException) as exc_info:
                await confirm_tool(req, db=MagicMock(), current_user=_make_user(100))

        assert exc_info.value.error_code == "NOT_CONFIRMATION_OWNER"
        wake.assert_not_awaited()


# ============ 修订 S-13：用户被自动禁用 ============


class TestUserDisabled:
    """修订 S-13：HITL 期间用户被自动禁用 → 403 AI_USER_DISABLED"""

    async def test_disabled_user_blocked(self) -> None:
        """legacy approve 被禁用后写 expired 终态并清理离线 pending。"""
        pending = _make_pending(
            user_id=100,
            source_user_message_id=987,
            guard_owner_token="owner-token",
        )
        fake_log = SimpleNamespace(log_id=12345)
        db = MagicMock()
        db.commit = AsyncMock()
        with (
            patch(
                "app.modules.ai.api.confirm.hitl_manager.get_pending",
                AsyncMock(return_value=pending),
            ),
            patch(
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=True),  # 用户已被禁用
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                AsyncMock(return_value=fake_log),
            ),
            patch(
                "app.modules.ai.api.confirm.operation_log_service.mark_expired_if_pending",
                AsyncMock(return_value=fake_log),
            ) as mark_expired,
            patch(
                "app.modules.ai.api.confirm.chat_run_finalizer.finalize_pending_turn",
                AsyncMock(),
            ) as finalize,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.wake",
                AsyncMock(return_value=False),
            ) as wake,
            patch(
                "app.modules.ai.api.confirm.chat_run_guard.release",
                AsyncMock(),
            ) as release,
            patch(
                "app.modules.ai.api.confirm.hitl_manager.delete_pending",
                AsyncMock(),
            ) as delete_pending,
        ):
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approve")
            with pytest.raises(AuthorizationException) as exc_info:
                await confirm_tool(req, db=db, current_user=_make_user(100))

        assert exc_info.value.error_code == "AI_USER_DISABLED"
        mark_expired.assert_awaited_once_with(
            db,
            fake_log.log_id,
            error_code="AI_USER_DISABLED",
            tenant=ANY,
        )
        finalize.assert_awaited_once_with(
            db,
            pending=pending,
            ok=False,
            error_code="AI_USER_DISABLED",
            error_msg="AI 已被禁用，操作未执行",
            tenant=ANY,
        )
        db.commit.assert_awaited_once()
        wake.assert_awaited_once_with(
            "cid_test_0123", ConfirmAction.REJECTED, tenant=ANY
        )
        release.assert_awaited_once_with(
            ANY,
            conversation_id=pending.conversation_id,
            owner_token="owner-token",
            tenant=ANY,
        )
        delete_pending.assert_awaited_once_with(ANY, "cid_test_0123", tenant=ANY)

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
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approve")
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
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approve")
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
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approve")
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
            "tenant": ANY,
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
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approve")
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
            req = ConfirmRequest(confirmationId="cid_test_0123", action="approve")
            db_mock = MagicMock()
            db_mock.commit = AsyncMock()
            result = await confirm_tool(req, db=db_mock, current_user=_make_user(100))

        # 仍返回 stream_gone（mark_expired 失败被吞掉）
        assert result.data.status == "stream_gone"
