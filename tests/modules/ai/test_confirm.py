"""Historical HITL helper protocol tests; production boundary has real DB tests.

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

from app.core.base_response import ResponseModel
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import TenantContext
from app.modules.ai.agents.hitl.constants import ConfirmAction
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.registry import RegisteredTool
from app.modules.ai.api.confirm import _confirm_prepared as legacy_confirm_prepared
from app.modules.ai.api.confirm import _legacy_confirm_tool as confirm_tool
from app.modules.ai.api.confirm import _notify_prepared_terminal
from app.modules.ai.schemas.confirm import ConfirmRequest, ConfirmResponse
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    assert_success,
    count_real_handler,
    observe,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)


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
        execution_owner=(
            "v0162-file:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            if status == "running"
            else None
        ),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        guard_owner_token=None,
        trace_id="tr_test",
        agent_code="user_mgmt",
        data_scope_hash=data_scope_hash,
        command_action="send",
        risk_level="high",
        chip_target=None,
        frozen_args={"preview_token": "server-secret"},
        args_hash="a" * 64,
        snapshot_hash="b" * 64,
        snapshot={},
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


def _make_registered_write_tool(action) -> RegisteredTool:  # noqa: ANN001
    return RegisteredTool(
        meta=AiToolMeta(
            name=action.execute_tool_name,
            agent=action.agent_code,
            summary="Confirm transaction contract fixture",
            required_perms=("system:user:add",),
            risk="high",
            readonly=False,
        ),
        fn=AsyncMock(),
    )


def _completion_response(action):  # noqa: ANN001
    return ResponseModel.success(
        data=ConfirmResponse(
            actionId=action.action_id,
            toolCallId=action.execute_tool_call_id,
            status=action.status,
        )
    )


def _make_db():
    """Request-session IO stays awaitable when entry guards issue real SQL."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.invalidate = AsyncMock()
    db.close = AsyncMock()
    return db


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
def _mock_external(request):
    if "completion_case" in request.fixturenames:
        yield
        return
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
        mock_session_ctx = _make_db()
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
            db_mock = _make_db()
            db_mock.commit = AsyncMock()

            result = await confirm_tool(req, db=db_mock, current_user=_make_user(100))

        assert result.code == 200
        assert result.data.tool_call_id == "tc_test"
        assert result.data.status == "queued"

    async def test_legacy_approve_without_permission_expires_and_cleans_pending(
        self,
    ) -> None:
        log = SimpleNamespace(log_id=77, result_summary=None)
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
            db = _make_db()
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
    async def test_legacy_readonly_commit_timeout_quarantines_before_dependency_teardown(
        self, monkeypatch
    ):
        import asyncio

        from app.db import session as session_module
        from app.modules.ai.agents.gateway import atomic_execution

        pending = _make_prepared_action(interaction_flow="direct")
        approved = _make_prepared_action(status="approved", interaction_flow="direct")
        running = _make_prepared_action(status="running", interaction_flow="direct")
        for action in (pending, approved, running):
            action.execute_tool_name = "user.count"
            action.execution_owner = None
        registered = RegisteredTool(
            meta=AiToolMeta(
                name="user.count",
                agent="user_mgmt",
                summary="Readonly confirmation",
                required_perms=("system:user:list",),
                risk="low",
                readonly=True,
            ),
            fn=AsyncMock(),
        )
        db = _make_db()
        db.info = {}
        cancelled, release = asyncio.Event(), asyncio.Event()

        async def delayed_commit():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        db.commit = delayed_commit
        monkeypatch.setattr(atomic_execution, "_COMMIT_TIMEOUT_SEC", 0.01)
        monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: db)
        dependency = session_module.get_db(SimpleNamespace(state=SimpleNamespace()))
        assert await anext(dependency) is db
        try:
            with (
                patch(
                    "app.modules.ai.agents.tools.registry.ToolRegistry.get",
                    return_value=SimpleNamespace(find=lambda _name: registered),
                ),
                patch(
                    "app.modules.ai.api.confirm.prepared_action_service.get_by_confirmation_id",
                    AsyncMock(return_value=pending),
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
                    AsyncMock(side_effect=[approved, running]),
                ),
                patch(
                    "app.modules.ai.api.confirm.operation_log_service.get_by_tool_call_id",
                    AsyncMock(return_value=None),
                ),
                patch(
                    "app.modules.ai.api.confirm.chat_service.build_chat_deps",
                    AsyncMock(return_value=SimpleNamespace(data_scope_hash=None)),
                ),
                patch(
                    "app.modules.ai.api.confirm.check_user_disabled",
                    AsyncMock(return_value=False),
                ),
                patch(
                    "app.modules.ai.api.confirm.validate_prepared_execution",
                    return_value=registered,
                ),
                patch(
                    "app.modules.ai.api.confirm.execute_approved_prepared_action",
                    AsyncMock(),
                ) as execute,
            ):
                with pytest.raises(TimeoutError) as error:
                    await legacy_confirm_prepared(
                        ConfirmRequest(
                            confirmationId=pending.confirmation_id, action="approve"
                        ),
                        db=db,
                        current_user=_make_user(),
                        tenant=_make_user()._tenant_context,
                        action_ref=pending,
                    )
            await asyncio.wait_for(cancelled.wait(), 1)
            assert session_module.is_session_quarantined(db)
            db.invalidate.assert_not_awaited()
            with pytest.raises(TimeoutError):
                await dependency.athrow(error.value)
            db.rollback.assert_not_awaited()
            db.close.assert_not_awaited()
            execute.assert_not_awaited()
        finally:
            release.set()
            commit = db.info.get(atomic_execution._COMMIT_TASK_KEY)
            if commit is not None:
                await asyncio.gather(commit, return_exceptions=True)
            entry = atomic_execution._QUARANTINED_SESSIONS.get(id(db))
            if entry is not None:
                await entry[1]
            await dependency.aclose()
        db.invalidate.assert_awaited_once()

    @pytest.mark.parametrize("state", ["succeeded", "failed", "rejected", "expired"])
    async def test_legacy_readonly_injection_confirmation_preserves_terminal_state(
        self, state
    ) -> None:
        """An injection-triggered HITL read never acquires a write protocol marker."""
        action = _make_prepared_action(status=state)
        action.execute_tool_name = "user.count"
        action.execution_owner = None
        registered = SimpleNamespace(meta=SimpleNamespace(readonly=True))
        with (
            patch(
                "app.modules.ai.agents.tools.registry.ToolRegistry.get",
                return_value=SimpleNamespace(find=lambda _name: registered),
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
                "app.modules.ai.api.confirm.check_user_disabled",
                AsyncMock(return_value=False),
            ),
            patch(
                "app.modules.ai.api.confirm.execute_approved_prepared_action",
                AsyncMock(),
            ) as execute,
        ):
            response = await legacy_confirm_prepared(
                ConfirmRequest(confirmationId=action.confirmation_id, action="approve"),
                db=_make_db(),
                current_user=_make_user(),
                tenant=_make_user()._tenant_context,
                action_ref=action,
            )
        assert response.data.status == state
        execute.assert_not_awaited()

    async def test_readonly_failed_waiter_preserves_tool_failure(self) -> None:
        from app.modules.ai.agents.gateway import executor

        action = _make_prepared_action(status="failed")
        action.execute_tool_name = "user.count"
        action.execution_owner = None
        action.error_code = "AI_TOOL_TIMEOUT"
        with (
            patch(
                "app.modules.ai.agents.tools.registry.ToolRegistry.get",
                return_value=SimpleNamespace(
                    find=lambda _name: SimpleNamespace(
                        meta=SimpleNamespace(readonly=True)
                    )
                ),
            ),
            patch.object(
                executor,
                "_get_prepared_action_by_confirmation",
                AsyncMock(return_value=action),
            ),
        ):
            result, _ = await executor._load_prepared_terminal_result(
                action.confirmation_id, tenant=_make_user()._tenant_context
            )
        assert result.ok is False
        assert result.error_code == "AI_TOOL_TIMEOUT"

    async def test_reject_bypasses_revoked_chat_permission_and_disabled_state(
        self, completion_case, monkeypatch
    ):
        from app.modules.ai.api import confirm
        from app.modules.ai.service import (
            authorization_execution_candidate as candidate,
        )

        denied = MagicMock(
            side_effect=AuthorizationException(error_code="AI_CHAT_PERMISSION_DENIED")
        )
        disabled = AsyncMock(return_value=True)
        monkeypatch.setattr(candidate, "ensure_ai_chat_use", denied)
        monkeypatch.setattr(confirm, "check_user_disabled", disabled)
        result = await approve(completion_case, decision="reject")
        assert result.data.status == "rejected"
        denied.assert_not_called()
        disabled.assert_not_awaited()
        assert completion_case.calls == []

    async def test_approve_without_chat_permission_terminalizes_before_execution(
        self, completion_case, monkeypatch
    ):
        from app.modules.ai.api import confirm
        from app.modules.ai.service import (
            authorization_execution_candidate as candidate,
        )

        monkeypatch.setattr(
            candidate,
            "ensure_ai_chat_use",
            MagicMock(
                side_effect=AuthorizationException(
                    error_code="AI_CHAT_PERMISSION_DENIED"
                )
            ),
        )
        observations = []

        async def notify(*_args, **_kwargs):
            observations.append(await observe(completion_case))

        monkeypatch.setattr(confirm, "_notify_prepared_terminal", notify)
        with pytest.raises(AuthorizationException) as error:
            await approve(completion_case)
        assert error.value.error_code == "AI_CHAT_PERMISSION_DENIED"
        assert len(observations) == 1
        assert (
            observations[0]["action_status"]
            == observations[0]["operation_status"]
            == "expired"
        )
        assert completion_case.calls == []

    async def test_stale_snapshot_is_rejected_before_wake(
        self, completion_case, monkeypatch
    ):
        from app.modules.ai.api import confirm
        from app.modules.system.service.authorization_ai_tool_adapter import (
            system_ai_tool_adapter,
        )

        stale = BusinessRuleException(
            "preview snapshot changed", error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE"
        )
        monkeypatch.setattr(
            system_ai_tool_adapter, "validate_planned", AsyncMock(side_effect=stale)
        )
        observations = []

        async def notify(*_args, **_kwargs):
            observations.append(await observe(completion_case))

        monkeypatch.setattr(confirm, "_notify_prepared_terminal", notify)
        with pytest.raises(BusinessRuleException) as error:
            await approve(completion_case)
        assert error.value.error_code == stale.error_code
        state = await observe(completion_case)
        assert state["action_status"] == state["operation_status"] == "expired"
        assert len(observations) == 1 and observations[0]["action_status"] == "expired"
        assert completion_case.calls == [] and state["user_ids"] == []

    async def test_reject_does_not_require_business_snapshot_to_stay_fresh(
        self, completion_case, monkeypatch
    ):
        from app.modules.system.service.authorization_ai_tool_adapter import (
            system_ai_tool_adapter,
        )

        validate = AsyncMock(
            side_effect=BusinessRuleException(
                "stale", error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE"
            )
        )
        monkeypatch.setattr(system_ai_tool_adapter, "validate_planned", validate)
        assert (
            await approve(completion_case, decision="reject")
        ).data.status == "rejected"
        validate.assert_not_awaited()
        assert completion_case.calls == []

    @pytest.mark.parametrize(
        ("approval", "dispatch"),
        [("hitl", "inline"), ("required", "execute_after_approval")],
    )
    async def test_approve_executes_inline_without_redis_pending(
        self, completion_case, monkeypatch, approval, dispatch
    ):
        from app.modules.ai.api import confirm
        from app.modules.ai.models.prepared_action import AiPreparedAction
        from app.modules.system.service.authorization_ai_tool_adapter import (
            system_ai_tool_adapter,
        )
        from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal

        case = completion_case
        async with AsyncSessionLocal() as db, db.begin():
            action = await db.get(AiPreparedAction, case.action_id)
            action.approval_mode, action.dispatch_mode = approval, dispatch
        count_real_handler(case, monkeypatch)
        real = system_ai_tool_adapter.apply
        claims = []

        async def apply(*args, **kwargs):
            claims.append(await observe(case))
            return await real(*args, **kwargs)

        monkeypatch.setattr(system_ai_tool_adapter, "apply", apply)
        pending = AsyncMock(
            side_effect=AssertionError("Durable confirm cannot depend on Redis pending")
        )
        monkeypatch.setattr(confirm.hitl_manager, "get_pending", pending)
        response = await approve(case)
        assert response.data.status == "succeeded"
        assert len(claims) == 1 and claims[0]["action_status"] == "running"
        assert len(case.calls) == 1
        assert_success(case, await observe(case))
        pending.assert_not_awaited()

    async def test_unknown_inline_execution_does_not_publish_failure(
        self, completion_case, monkeypatch
    ):
        from app.modules.ai.api import confirm
        from tests.modules.ai.test_completion_resume_guards import leave_claim_running

        notify = AsyncMock()
        monkeypatch.setattr(confirm, "_notify_prepared_terminal", notify)
        await leave_claim_running(completion_case, monkeypatch)
        state = await observe(completion_case)
        assert state["action_status"] == state["operation_status"] == "running"
        assert completion_case.calls == [] and state["user_ids"] == []
        notify.assert_not_awaited()

    async def test_duplicate_approve_returns_terminal_without_execution(
        self, completion_case, monkeypatch
    ):
        count_real_handler(completion_case, monkeypatch)
        assert (await approve(completion_case)).data.status == "succeeded"
        assert (await approve(completion_case)).data.status == "succeeded"
        assert len(completion_case.calls) == 1
        assert_success(completion_case, await observe(completion_case))

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
            db_mock = _make_db()
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
                await confirm_tool(req, db=_make_db(), current_user=_make_user(100))
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
                await confirm_tool(req, db=_make_db(), current_user=_make_user(999))
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
                await confirm_tool(req, db=_make_db(), current_user=_make_user(100))

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
        db = _make_db()
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
                    db=_make_db(),
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
                req, db=_make_db(), current_user=_make_user(100)
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
            db_mock = _make_db()
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
            db_mock = _make_db()
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
            db_mock = _make_db()
            db_mock.commit = AsyncMock()
            result = await confirm_tool(req, db=db_mock, current_user=_make_user(100))

        # 仍返回 stream_gone（mark_expired 失败被吞掉）
        assert result.data.status == "stream_gone"
