"""build_data_scope_context 单元测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §6.2。

super_admin / DATA_SCOPE_ALL 路径不依赖 db，做纯单元测试。
其它 scope 路径需要 user / role / dept 关系数据，留 Plan 1.5 鉴权矩阵测试覆盖。
"""

# ruff: noqa: ARG001, ARG005, PLC0415  test 函数局部 monkeypatch + 占位参数

from unittest.mock import MagicMock

import pytest

from app.constants import DATA_SCOPE_ALL
from app.modules.ai.core import data_scope_loader as loader_mod
from app.modules.ai.core.context import DataScopeContext
from app.modules.ai.core.data_scope_loader import build_data_scope_context


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
    """统一 monkeypatch loader_mod 的依赖"""
    defaults: dict[str, object] = {
        "is_super_admin": lambda u: False,
        "get_best_scope": lambda u: DATA_SCOPE_ALL,
        "get_user_data_scope_filters": _async_return([]),
        "get_custom_dept_ids": _async_return([]),
        "get_dept_and_sub_ids": _async_return([]),
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
        """spec §6.2: 超管 → accessible_*_ids=None + filters=[]"""
        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)

        ctx = await build_data_scope_context(MagicMock(), mock_super_admin)

        assert isinstance(ctx, DataScopeContext)
        assert ctx.accessible_dept_ids is None
        assert ctx.accessible_user_ids is None
        assert ctx.filters == []

    async def test_data_scope_all_returns_all_visible(
        self, mock_normal_user: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spec §6.2: DATA_SCOPE_ALL → accessible_*_ids=None + filters=[]"""
        _patch(monkeypatch)  # is_super_admin=False, get_best_scope=ALL

        ctx = await build_data_scope_context(MagicMock(), mock_normal_user)

        assert ctx.accessible_dept_ids is None
        assert ctx.accessible_user_ids is None
        assert ctx.filters == []


class TestBuildDataScopeContextStructure:
    """验证返回类型是 DataScopeContext（spec §4.6 契约）"""

    async def test_returns_data_scope_context_instance(
        self, mock_super_admin: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loader_mod, "is_super_admin", lambda u: True)

        ctx = await build_data_scope_context(MagicMock(), mock_super_admin)

        assert isinstance(ctx, DataScopeContext)
        assert hasattr(ctx, "accessible_dept_ids")
        assert hasattr(ctx, "accessible_user_ids")
        assert hasattr(ctx, "filters")
