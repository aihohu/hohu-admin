"""ChatDeps / AiToolContext / build_tool_context / DataScopeContext 单元测试

覆盖 ChatDeps 与 AiToolContext 的构造和转换。
"""

# ruff: noqa: ARG001, ARG005  test 函数 ctx / kwargs 是与生产签名一致的占位

import dataclasses
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import literal_column, select
from tenant_helpers import tenant_context

from app.core.tenant import TenantContext
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


def _make_data_scope(tenant: TenantContext | None = None) -> DataScopeContext:
    tenant = tenant or tenant_context()
    return DataScopeContext(
        tenant=tenant,
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
    principal = user or MagicMock()
    principal.user_id = 1
    tenant = tenant_context(tenant_id=tenant_id, actor_user_id=principal.user_id)
    return ChatDeps(
        user=principal,
        perms=perms or {"system:user:list"},
        db=MagicMock(),
        data_scope=_make_data_scope(tenant),
        agent=MagicMock(),
        trace_id=trace_id,
        tenant=tenant,
    )


# ============ DataScopeContext ============


class TestDataScopeContext:
    def test_default_filters_empty_list(self) -> None:
        """filters 默认空 list（不是 None），与 accessible_*.None 语义区分"""
        scope = DataScopeContext(
            tenant=tenant_context(),
            accessible_dept_ids=None,
            accessible_user_scope=None,
        )
        assert scope.filters == []
        assert scope.accessible_dept_ids is None
        assert scope.accessible_user_scope is None

    def test_all_visible_means_none(self) -> None:
        """None 表示全部可见（超管 / DATA_SCOPE_ALL），不是无可见"""
        scope = DataScopeContext(
            tenant=tenant_context(),
            accessible_dept_ids=None,
            accessible_user_scope=None,
        )
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
        """trace_id 必填且无默认值，防止空值进入数据库索引。"""
        fields = {f.name: f for f in dataclasses.fields(ChatDeps)}
        assert fields["trace_id"].default is dataclasses.MISSING


# ============ AiToolContext ============


class TestAiToolContext:
    def test_secrets_default_empty(self) -> None:
        user = MagicMock()
        user.user_id = 1
        tenant = tenant_context()
        ctx = AiToolContext(
            user=user,
            perms=set(),
            db=MagicMock(),
            data_scope=_make_data_scope(tenant),
            trace_id="tr_x",
            tool_meta=_make_meta(),
            tenant=tenant,
        )
        assert ctx.secrets == {}  # 当前默认不注入 secrets。
        assert ctx.tenant_id == 0

    def test_tool_meta_required(self) -> None:
        """聚合工具通过 ctx.tool_meta 读取分组和过滤白名单。"""
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
        user = MagicMock()
        user.user_id = 1
        tenant = tenant_context()
        ctx = AiToolContext(
            user=user,
            perms=set(),
            db=MagicMock(),
            data_scope=_make_data_scope(tenant),
            trace_id="tr_x",
            tool_meta=meta_with_aggregation,
            tenant=tenant,
        )
        assert ctx.tool_meta.max_groups == 15
        assert ctx.tool_meta.allowed_group_by == ("user_gender", "status")
        assert ctx.tool_meta is meta_with_aggregation


# ============ build_tool_context ============


class TestBuildContext:
    def test_basic_conversion(self) -> None:
        """ChatDeps 转 AiToolContext 时替换 db、注入 tool_meta 并复用其他字段。"""
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
        """trace_id 必须非空，防止空字符串进入数据库索引。"""
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
