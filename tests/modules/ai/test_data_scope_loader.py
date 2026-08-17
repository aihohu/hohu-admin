"""build_data_scope_context 单元测试

覆盖 AI 工具数据范围上下文构造。

super_admin / DATA_SCOPE_ALL 路径不依赖 db，做纯单元测试。
其它 scope 路径需要 user / role / dept 关系数据，留 Plan 1.5 鉴权矩阵测试覆盖。
"""

# ruff: noqa: ARG001, ARG005, PLC0415  test 函数局部 monkeypatch + 占位参数

from unittest.mock import MagicMock

import pytest

from app.modules.ai.core import data_scope_loader as loader_mod
from app.modules.ai.core.context import DataScopeContext
from app.modules.ai.core.data_scope_loader import build_data_scope_context
from app.utils.data_scope import DataScopeResolution


@pytest.fixture
def mock_super_admin() -> MagicMock:
    """构造 is_super_admin(user) → True 的 mock user"""
    user = MagicMock()
    user.user_id = 1
    user.roles = []
    user.depts = []
    return user


@pytest.fixture
def mock_normal_user() -> MagicMock:
    """构造 is_super_admin(user) → False 的 mock user（具体 scope 由测试设置）"""
    user = MagicMock()
    user.user_id = 100
    user.roles = []
    user.depts = []
    return user


def _patch(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Patch the single shared resolver consumed by the AI adapter."""
    defaults: dict[str, object] = {
        "resolve_data_scope": _async_return(
            DataScopeResolution(
                scope_kinds=frozenset({"1"}),
                accessible_dept_ids=None,
                accessible_user_scope=None,
                include_self=True,
                unbounded=True,
            )
        ),
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(loader_mod, name, value)


def _async_return(value):
    async def _impl(*args, **kwargs):
        return value

    return _impl


class TestBuildDataScopeContextSuperAdmin:
    async def test_super_admin_returns_all_visible(
        self, mock_super_admin: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超级管理员返回无限制 ID 和空过滤器。"""
        _patch(monkeypatch)

        ctx = await build_data_scope_context(MagicMock(), mock_super_admin)

        assert isinstance(ctx, DataScopeContext)
        assert ctx.accessible_dept_ids is None
        assert ctx.accessible_user_scope is None
        assert ctx.filters == []

    async def test_data_scope_all_returns_all_visible(
        self, mock_normal_user: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DATA_SCOPE_ALL 返回无限制 ID 和空过滤器。"""
        _patch(monkeypatch)

        ctx = await build_data_scope_context(MagicMock(), mock_normal_user)

        assert ctx.accessible_dept_ids is None
        assert ctx.accessible_user_scope is None
        assert ctx.filters == []


class TestBuildDataScopeContextStructure:
    """返回类型应为 DataScopeContext。"""

    async def test_returns_data_scope_context_instance(
        self, mock_super_admin: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch)

        ctx = await build_data_scope_context(MagicMock(), mock_super_admin)

        assert isinstance(ctx, DataScopeContext)
        assert hasattr(ctx, "accessible_dept_ids")
        assert hasattr(ctx, "accessible_user_scope")
        assert hasattr(ctx, "filters")
