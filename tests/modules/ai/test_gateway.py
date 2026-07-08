"""Gateway Executor + ensure_targets_in_scope + ToolResult 单元测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §6.1 / §6.2 / §6.5。

execute_tool 测试用 db_session fixture（ai/conftest.py），不用 mock AsyncSessionLocal。
"""

# ruff: noqa: ARG001, ARG005, PLC0415

from typing import Any
from unittest.mock import MagicMock

import pytest
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
    accessible_user_ids: set[int] | None = None,
    accessible_dept_ids: set[int] | None = None,
) -> AiToolContext:
    data_scope = DataScopeContext(
        accessible_dept_ids=accessible_dept_ids,
        accessible_user_ids=accessible_user_ids,
        filters=[],
    )
    meta = AiToolMeta(
        name="test.tool",
        agent="user_mgmt",
        summary="x",
        required_perms=("p",),
        risk="low",
    )
    return AiToolContext(
        user=MagicMock(user_id=1),
        perms={"p"},
        db=MagicMock(),
        data_scope=data_scope,
        trace_id="tr_test",
        tool_meta=meta,
    )


class TestEnsureTargetsInScope:
    def test_all_visible_skips_check(self) -> None:
        ctx = _make_ctx(accessible_user_ids=None, accessible_dept_ids=None)
        ensure_targets_in_scope(ctx, user_ids=[999999])
        ensure_targets_in_scope(ctx, dept_ids=[888888])

    def test_empty_list_skips_check(self) -> None:
        ctx = _make_ctx(accessible_user_ids={1, 2, 3})
        ensure_targets_in_scope(ctx, user_ids=[])

    def test_none_targets_skips_check(self) -> None:
        ctx = _make_ctx(accessible_user_ids={1, 2, 3}, accessible_dept_ids={10})
        ensure_targets_in_scope(ctx)

    def test_user_ids_in_scope_passes(self) -> None:
        ctx = _make_ctx(accessible_user_ids={1, 2, 3})
        ensure_targets_in_scope(ctx, user_ids=[1, 2])

    def test_user_ids_out_of_scope_raises(self) -> None:
        ctx = _make_ctx(accessible_user_ids={1, 2, 3})
        with pytest.raises(AuthorizationException) as exc_info:
            ensure_targets_in_scope(ctx, user_ids=[1, 99])
        assert exc_info.value.error_code == "AI_DATA_SCOPE_VIOLATION"

    def test_dept_ids_out_of_scope_raises(self) -> None:
        ctx = _make_ctx(accessible_user_ids={1, 2, 3}, accessible_dept_ids={10, 20})
        with pytest.raises(AuthorizationException) as exc_info:
            ensure_targets_in_scope(ctx, dept_ids=[10, 99])
        assert exc_info.value.error_code == "AI_DATA_SCOPE_VIOLATION"

    def test_create_bys_uses_user_ids_scope(self) -> None:
        ctx = _make_ctx(accessible_user_ids={1, 2, 3}, accessible_dept_ids={10})
        with pytest.raises(AuthorizationException):
            ensure_targets_in_scope(ctx, create_bys=[1, 99])

    def test_multi_dim_targets_all_must_pass(self) -> None:
        ctx = _make_ctx(accessible_user_ids={1, 2, 3}, accessible_dept_ids={10, 20})
        with pytest.raises(AuthorizationException):
            ensure_targets_in_scope(ctx, user_ids=[1], dept_ids=[99])


# ============ execute_tool ============


def _make_deps(
    *,
    perms: set[str] | None = None,
    db: Any = None,
) -> ChatDeps:
    return ChatDeps(
        user=MagicMock(user_id=1),
        perms=perms if perms is not None else {"system:user:list"},
        db=db or MagicMock(),
        data_scope=DataScopeContext(None, None, []),
        agent=MagicMock(),
        trace_id="tr_test",
    )


class TestExecuteTool:
    async def test_tool_not_found(self, db_session: AsyncSession) -> None:
        """spec §6.1: LLM 幻觉调用了不存在的 tool"""
        deps = _make_deps(db=db_session)
        result = await execute_tool("missing.tool", {}, deps)
        assert result.ok is False
        assert result.error_code == "AI_TOOL_NOT_FOUND"

    async def test_perm_denied(self, db_session: AsyncSession) -> None:
        """spec §6.1: tool 存在但用户 perms 不满足"""

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

    async def test_success_calls_tool_fn(self, db_session: AsyncSession) -> None:
        """spec §6.3: 独立 session + 调业务函数 + 返回 ToolResult.success"""

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
        """spec §6.5: 业务异常 → ToolResult.failure(原 error_code)"""

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
        """spec §6.5: AuthorizationException → ToolResult.failure(AI_DATA_SCOPE_VIOLATION)"""

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
        """spec §6.5: NotFoundException → ToolResult.failure(原 error_code)"""

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
        """spec §6.5: 未预期异常 → AI_INTERNAL_ERROR（保留异常类型名便于 debug）"""

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
