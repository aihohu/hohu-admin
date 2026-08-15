"""Gateway Executor + ensure_targets_in_scope + ToolResult 单元测试

覆盖网关工具查找、权限、数据范围和错误映射。

execute_tool 测试用 db_session fixture（ai/conftest.py），不用 mock AsyncSessionLocal。
ensure_targets_in_scope 使用 SQL count 路径，用户维度 mock ctx.db.execute。
"""

# ruff: noqa: ARG001, ARG005, PLC0415

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.modules.ai.agents.gateway import (
    ToolResult,
    ensure_targets_in_scope,
    execute_tool,
)
from app.modules.ai.agents.tools import (
    AiToolMeta,
    ToolRegistry,
    ai_tool,
)
from app.modules.ai.core.context import (
    AiToolContext,
    ChatDeps,
    DataScopeContext,
)


@pytest.fixture(autouse=True)
def reset_registry():
    ToolRegistry.reset()
    yield
    ToolRegistry.reset()


# ============ ToolResult ============


class TestToolResult:
    def test_success_factory(self) -> None:
        r = ToolResult.success({"count": 5}, duration_ms=42)
        assert r.ok is True
        assert r.data == {"count": 5}
        assert r.meta == {"duration_ms": 42}

    def test_failure_factory(self) -> None:
        r = ToolResult.failure("AI_DATA_SCOPE_VIOLATION", "目标不在范围", tool="x")
        assert r.ok is False
        assert r.error_code == "AI_DATA_SCOPE_VIOLATION"


# ============ ensure_targets_in_scope ============


def _make_ctx(
    *,
    accessible_user_scope: Select[tuple[int]] | None = None,
    accessible_dept_ids: set[int] | None = None,
    visible_count: int = 0,
) -> AiToolContext:
    """构造测试用 AiToolContext。visible_count 模拟 SQL count(*) 返回的可见目标数。"""
    data_scope = DataScopeContext(
        accessible_dept_ids=accessible_dept_ids,
        accessible_user_scope=accessible_user_scope,
        filters=[],
    )
    meta = AiToolMeta(
        name="test.tool",
        agent="user_mgmt",
        summary="x",
        required_perms=("p",),
        risk="low",
    )
    # mock db.execute → 返 mock result → scalar_one 返 visible_count
    mock_result = MagicMock()
    mock_result.scalar_one = MagicMock(return_value=visible_count)
    mock_db = MagicMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    return AiToolContext(
        user=MagicMock(user_id=1),
        perms={"p"},
        db=mock_db,
        data_scope=data_scope,
        trace_id="tr_test",
        tool_meta=meta,
    )


_DUMMY_SCOPE: Select[tuple[int]] = select(
    __import__("sqlalchemy").literal_column("0").label("user_id")
)


class TestEnsureTargetsInScope:
    async def test_all_visible_skips_check(self) -> None:
        ctx = _make_ctx(accessible_user_scope=None, accessible_dept_ids=None)
        await ensure_targets_in_scope(ctx, user_ids=[999999])
        await ensure_targets_in_scope(ctx, dept_ids=[888888])

    async def test_empty_list_skips_check(self) -> None:
        ctx = _make_ctx(accessible_user_scope=_DUMMY_SCOPE)
        await ensure_targets_in_scope(ctx, user_ids=[])

    async def test_none_targets_skips_check(self) -> None:
        ctx = _make_ctx(accessible_user_scope=_DUMMY_SCOPE, accessible_dept_ids={10})
        await ensure_targets_in_scope(ctx)

    async def test_user_ids_in_scope_passes(self) -> None:
        # 2 个 target 全在 scope 内 → visible_count=2 → 通过
        ctx = _make_ctx(accessible_user_scope=_DUMMY_SCOPE, visible_count=2)
        await ensure_targets_in_scope(ctx, user_ids=[1, 2])

    async def test_user_ids_out_of_scope_raises(self) -> None:
        # 2 个 target 仅 1 个在 scope 内 → visible_count=1 < 2 → 抛异常
        ctx = _make_ctx(accessible_user_scope=_DUMMY_SCOPE, visible_count=1)
        with pytest.raises(AuthorizationException) as exc_info:
            await ensure_targets_in_scope(ctx, user_ids=[1, 99])
        assert exc_info.value.error_code == "AI_DATA_SCOPE_VIOLATION"

    async def test_dept_ids_out_of_scope_raises(self) -> None:
        # dept_ids 仍走 set 内存检查（不走 SQL，不需要 visible_count）
        ctx = _make_ctx(
            accessible_user_scope=_DUMMY_SCOPE, accessible_dept_ids={10, 20}
        )
        with pytest.raises(AuthorizationException) as exc_info:
            await ensure_targets_in_scope(ctx, dept_ids=[10, 99])
        assert exc_info.value.error_code == "AI_DATA_SCOPE_VIOLATION"

    async def test_create_bys_uses_user_scope(self) -> None:
        # create_bys 使用 accessible_user_scope；两个目标仅一个可见时拒绝。
        ctx = _make_ctx(
            accessible_user_scope=_DUMMY_SCOPE,
            accessible_dept_ids={10},
            visible_count=1,
        )
        with pytest.raises(AuthorizationException):
            await ensure_targets_in_scope(ctx, create_bys=[1, 99])

    async def test_multi_dim_targets_all_must_pass(self) -> None:
        # user 在 scope，dept 越界 → 抛异常
        ctx = _make_ctx(
            accessible_user_scope=_DUMMY_SCOPE,
            accessible_dept_ids={10, 20},
            visible_count=1,
        )
        with pytest.raises(AuthorizationException):
            await ensure_targets_in_scope(ctx, user_ids=[1], dept_ids=[99])


# ============ execute_tool ============


def _make_deps(
    *,
    perms: set[str] | None = None,
    db: Any = None,
) -> ChatDeps:
    agent = MagicMock()
    agent.code = "user_mgmt"
    agent.enabled = True
    return ChatDeps(
        user=MagicMock(user_id=1),
        perms=perms if perms is not None else {"system:user:list"},
        db=db or MagicMock(),
        data_scope=DataScopeContext(None, None, []),
        agent=agent,
        trace_id="tr_test",
    )


class TestExecuteTool:
    async def test_tool_not_found(self, db_session: AsyncSession) -> None:
        """LLM 调用不存在的工具时返回稳定错误。"""
        deps = _make_deps(db=db_session)
        result = await execute_tool("missing.tool", {}, deps)
        assert result.ok is False
        assert result.error_code == "AI_TOOL_NOT_FOUND"

    async def test_perm_denied(self, db_session: AsyncSession) -> None:
        """工具存在但用户权限不足时拒绝执行。"""

        @ai_tool(
            AiToolMeta(
                name="user.perm_denied",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:add",),
                risk="low",
            )
        )
        async def _fn(ctx: Any) -> dict:
            return {"ok": True}

        deps = _make_deps(perms={"system:user:list"}, db=db_session)
        result = await execute_tool("user.perm_denied", {}, deps)
        assert result.ok is False
        assert result.error_code == "AI_TOOL_PERM_DENIED"

    async def test_runtime_agent_must_exactly_own_tool(
        self, db_session: AsyncSession
    ) -> None:
        called = False

        @ai_tool(
            AiToolMeta(
                name="user.agent_mismatch",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:list",),
                risk="low",
            )
        )
        async def _fn(ctx: Any) -> dict:
            nonlocal called
            called = True
            return {"ok": True}

        deps = _make_deps(db=db_session)
        deps.agent.code = "shared"
        result = await execute_tool("user.agent_mismatch", {}, deps)

        assert result.ok is False
        assert result.error_code == "AI_TOOL_AGENT_MISMATCH"
        assert called is False

    async def test_success_calls_tool_fn(self, db_session: AsyncSession) -> None:
        """使用独立 session 调用业务函数并返回 ToolResult.success。"""

        @ai_tool(
            AiToolMeta(
                name="user.ok",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:list",),
                risk="low",
            )
        )
        async def _fn(ctx: Any, x: int = 0, y: str = "") -> dict:
            return {"x": x, "y": y}

        deps = _make_deps(db=db_session)
        result = await execute_tool("user.ok", {"x": 1, "y": "foo"}, deps)
        assert result.ok is True
        assert result.data == {"x": 1, "y": "foo"}

    async def test_business_exception_translates(
        self, db_session: AsyncSession
    ) -> None:
        """业务异常映射为保留原 error_code 的 ToolResult.failure。"""

        @ai_tool(
            AiToolMeta(
                name="user.biz_err",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:list",),
                risk="low",
            )
        )
        async def _fn(ctx: Any) -> dict:
            raise BusinessRuleException("邮箱已存在", error_code="USER_EMAIL_DUPLICATE")

        deps = _make_deps(db=db_session)
        result = await execute_tool("user.biz_err", {}, deps)
        assert result.ok is False
        assert result.error_code == "USER_EMAIL_DUPLICATE"
        assert "邮箱已存在" in result.error_msg

    async def test_authorization_exception_translates(
        self, db_session: AsyncSession
    ) -> None:
        """AuthorizationException 映射为 AI_DATA_SCOPE_VIOLATION。"""

        @ai_tool(
            AiToolMeta(
                name="user.authz_err",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:list",),
                risk="low",
            )
        )
        async def _fn(ctx: Any) -> dict:
            raise AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION")

        deps = _make_deps(db=db_session)
        result = await execute_tool("user.authz_err", {}, deps)
        assert result.ok is False
        assert result.error_code == "AI_DATA_SCOPE_VIOLATION"

    async def test_not_found_exception_translates(
        self, db_session: AsyncSession
    ) -> None:
        """NotFoundException 映射为保留原 error_code 的失败结果。"""

        @ai_tool(
            AiToolMeta(
                name="user.nf_err",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:list",),
                risk="low",
            )
        )
        async def _fn(ctx: Any) -> dict:
            raise NotFoundException("用户", error_code="USER_NOT_FOUND")

        deps = _make_deps(db=db_session)
        result = await execute_tool("user.nf_err", {}, deps)
        assert result.ok is False
        assert result.error_code == "USER_NOT_FOUND"

    async def test_unexpected_exception_translates_to_internal_error(
        self, db_session: AsyncSession
    ) -> None:
        """未预期异常映射为 AI_INTERNAL_ERROR，并保留异常类型便于排障。"""

        @ai_tool(
            AiToolMeta(
                name="user.unexpected",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:list",),
                risk="low",
            )
        )
        async def _fn(ctx: Any) -> dict:
            raise RuntimeError("DB connection lost")

        deps = _make_deps(db=db_session)
        result = await execute_tool("user.unexpected", {}, deps)
        assert result.ok is False
        assert result.error_code == "AI_INTERNAL_ERROR"
        assert "RuntimeError" in result.error_msg


class TestSseSerializesUiAndChipTarget:
    """验证 SSE 结果包含 ui 和 chipTarget 字段。"""

    def test_tool_call_result_serializes_ui(self) -> None:
        import json

        from app.modules.ai.agents.gateway.result import UIResult
        from app.modules.ai.agents.hitl.events import (
            ToolCallResultEvent,
            event_to_sse_data,
        )

        ui = UIResult(
            view_type="rows_affected",
            view_data={"count": 2, "ids": ["u1", "u2"]},
            audit={"affected_user_ids": ["u1", "u2"]},
            label_key="ai.tool.user.batch_delete.result",
            label_params={"count": 2},
        )
        event = ToolCallResultEvent(
            tool="user.batch_delete",
            tool_call_id="tc_test1",
            ok=True,
            duration_ms=230,
            result={"deleted": 2},
            ui=ui,
            affected_rows=2,
        )
        payload = json.loads(event_to_sse_data(event))

        assert payload["ui"]["viewType"] == "rows_affected"
        assert payload["ui"]["viewData"]["count"] == 2
        assert payload["ui"]["audit"]["affected_user_ids"] == ["u1", "u2"]
        assert payload["ui"]["labelKey"] == "ai.tool.user.batch_delete.result"
        assert payload["ui"]["labelParams"] == {"count": 2}

    def test_tool_call_started_serializes_chip_target(self) -> None:
        import json

        from app.modules.ai.agents.hitl.events import (
            ToolCallStartedEvent,
            event_to_sse_data,
        )

        event = ToolCallStartedEvent(
            tool="user.count",
            tool_call_id="tc_test2",
            summary="count users",
            args={},
            risk="low",
            trace_id="trace_xxx",
            chip_target="/system/user",
        )
        payload = json.loads(event_to_sse_data(event))
        assert payload["chipTarget"] == "/system/user"

    def test_tool_call_result_without_ui_omits_field(self) -> None:
        """ui=None 时序列化后 ui 字段不出现（_compact_json 移除 None）。"""
        import json

        from app.modules.ai.agents.hitl.events import (
            ToolCallResultEvent,
            event_to_sse_data,
        )

        event = ToolCallResultEvent(
            tool="user.batch_delete",
            tool_call_id="tc_test3",
            ok=False,
            duration_ms=10,
            error_code="AI_DATA_SCOPE_VIOLATION",
            error_msg="target not in scope",
        )
        payload = json.loads(event_to_sse_data(event))
        assert "ui" not in payload


class TestExecutorIsinstanceBranch:
    """验证 executor 的标准结果和兼容结果双路径。"""

    async def test_executor_preserves_tool_result_when_business_returns_it(
        self, db_session: AsyncSession
    ) -> None:
        """业务方返回 ToolResult 时 executor 不 double-wrap，保留 ui。"""
        from app.modules.ai.agents.gateway.result import UIResult

        ui = UIResult(
            view_type="rows_affected",
            view_data={"count": 2},
            audit={"affected_user_ids": ["u1", "u2"]},
            label_key="ai.tool.user.batch_delete.result",
            label_params={"count": 2},
        )

        @ai_tool(
            AiToolMeta(
                name="user.tool_result_path",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:list",),
                risk="low",
            )
        )
        async def _fn(ctx: Any) -> ToolResult:
            return ToolResult.success(data={"deleted": 2}, ui=ui)

        deps = _make_deps(db=db_session)
        result = await execute_tool("user.tool_result_path", {}, deps)
        assert result.ok is True
        # ToolResult 直接保留，不被 double-wrap 成 {"deleted": 2} 外层 data
        assert result.data == {"deleted": 2}
        assert result.ui is not None
        assert result.ui.view_type == "rows_affected"
        assert result.ui.view_data == {"count": 2}

    async def test_executor_wraps_dict_return_fallback(
        self, db_session: AsyncSession
    ) -> None:
        """业务方返回 dict 时 executor fallback 包装为 ui=None 的 ToolResult。"""

        @ai_tool(
            AiToolMeta(
                name="user.dict_path",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:list",),
                risk="low",
            )
        )
        async def _fn(ctx: Any) -> dict:
            return {"deleted": 2}

        deps = _make_deps(db=db_session)
        result = await execute_tool("user.dict_path", {}, deps)
        assert result.ok is True
        assert result.data == {"deleted": 2}
        assert result.ui is None
