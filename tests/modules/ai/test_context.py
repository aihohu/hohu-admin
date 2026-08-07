"""ChatDeps / AiToolContext / build_tool_context / DataScopeContext 单元测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §4.6。
"""

# ruff: noqa: ARG001, ARG005  test 函数 ctx / kwargs 是与生产签名一致的占位

import dataclasses
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import literal_column, select

from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import (
    AiToolContext,
    ChatDeps,
    DataScopeContext,
    build_tool_context,
)


def _make_meta(name: str = "user.lookup") -> AiToolMeta:
    return AiToolMeta(
        name=name,
        agent="user_mgmt",
        summary=f"tool {name}",
        required_perms=("system:user:list",),
        risk="low",
    )


def _make_data_scope() -> DataScopeContext:
    return DataScopeContext(
        accessible_dept_ids={100, 200},
        accessible_user_scope=select(literal_column("0").label("user_id")),
        filters=[],
    )


def _make_deps(
    trace_id: str = "tr_abc123",
    user: Any = None,
    perms: set[str] | None = None,
    tenant_id: int = 0,
) -> ChatDeps:
    return ChatDeps(
        user=user or MagicMock(user_id=1, __str__=lambda self: "u"),
        perms=perms or {"system:user:list"},
        db=MagicMock(),
        data_scope=_make_data_scope(),
        agent=MagicMock(),
        trace_id=trace_id,
        tenant_id=tenant_id,
    )


# ============ DataScopeContext ============


class TestDataScopeContext:
    def test_default_filters_empty_list(self) -> None:
        """filters 默认空 list（不是 None），与 accessible_*.None 语义区分"""
        scope = DataScopeContext(accessible_dept_ids=None, accessible_user_scope=None)
        assert scope.filters == []
        assert scope.accessible_dept_ids is None
        assert scope.accessible_user_scope is None

    def test_all_visible_means_none(self) -> None:
        """None 表示全部可见（超管 / DATA_SCOPE_ALL），不是无可见"""
        scope = DataScopeContext(accessible_dept_ids=None, accessible_user_scope=None)
        assert scope.accessible_dept_ids is None


# ============ ChatDeps ============


class TestChatDeps:
    def test_required_fields(self) -> None:
        deps = _make_deps(trace_id="tr_xyz")
        assert deps.trace_id == "tr_xyz"
        assert deps.perms == {"system:user:list"}
        assert deps.data_scope.accessible_user_scope is not None
        assert deps.tenant_id == 0

    def test_no_default_trace_id(self) -> None:
        """trace_id 必填，无默认值（spec §4.6 防 "" 漏到 DB 索引）"""
        fields = {f.name: f for f in dataclasses.fields(ChatDeps)}
        assert fields["trace_id"].default is dataclasses.MISSING


# ============ AiToolContext ============


class TestAiToolContext:
    def test_secrets_default_empty(self) -> None:
        ctx = AiToolContext(
            user=MagicMock(),
            perms=set(),
            db=MagicMock(),
            data_scope=_make_data_scope(),
            trace_id="tr_x",
            tool_meta=_make_meta(),
        )
        assert ctx.secrets == {}  # MVP 留空（§7.2）
        assert ctx.tenant_id == 0

    def test_tool_meta_required(self) -> None:
        """聚合 tool 通过 ctx.tool_meta 读 max_groups / allowed_filters（§5.5）"""
        meta_with_aggregation = AiToolMeta(
            name="user.stats",
            agent="user_mgmt",
            summary="stats",
            required_perms=("system:user:list",),
            risk="low",
            readonly=True,
            allowed_filters=("status", "user_gender"),
            allowed_group_by=("user_gender", "status"),
            max_groups=15,
        )
        ctx = AiToolContext(
            user=MagicMock(),
            perms=set(),
            db=MagicMock(),
            data_scope=_make_data_scope(),
            trace_id="tr_x",
            tool_meta=meta_with_aggregation,
        )
        assert ctx.tool_meta.max_groups == 15
        assert ctx.tool_meta.allowed_group_by == ("user_gender", "status")
        assert ctx.tool_meta is meta_with_aggregation


# ============ build_tool_context ============


class TestBuildContext:
    def test_basic_conversion(self) -> None:
        """spec §4.6: ChatDeps → AiToolContext 替换 db + 注入 tool_meta + 复用其它"""
        deps = _make_deps(trace_id="tr_test_basic", tenant_id=37)
        tool_db = MagicMock()
        meta = _make_meta()

        ctx = build_tool_context(deps, tool_db, meta)

        # 替换 db
        assert ctx.db is tool_db
        assert ctx.db is not deps.db
        # 复用
        assert ctx.user is deps.user
        assert ctx.perms is deps.perms
        assert ctx.data_scope is deps.data_scope
        assert ctx.trace_id == "tr_test_basic"
        assert ctx.tenant_id == 37
        # 注入
        assert ctx.tool_meta is meta
        # agent 被丢弃（不在 AiToolContext 字段中）
        assert not hasattr(ctx, "agent")
        # secrets 留空
        assert ctx.secrets == {}

    def test_rejects_empty_trace_id(self) -> None:
        """spec §4.6: trace_id 必填非空，防 "" 漏到 DB 索引"""
        deps = _make_deps(trace_id="")
        with pytest.raises(AssertionError, match="trace_id"):
            build_tool_context(deps, MagicMock(), _make_meta())


# ============ Frozen 校验（dataclass 默认可变，但 AiToolMeta 是 frozen） ============


class TestFrozenContracts:
    def test_tool_meta_frozen(self) -> None:
        """AiToolMeta 是 frozen，build 后 ctx.tool_meta 不可变"""
        meta = _make_meta()
        with pytest.raises(FrozenInstanceError):
            meta.name = "mutated"  # type: ignore[misc]
