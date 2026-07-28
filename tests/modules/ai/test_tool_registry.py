"""ToolRegistry + @ai_tool 装饰器 单元测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5.1 / §5.4 / §5.5。

不依赖 DB（async db fixture 重），启动校验测试用 mock AsyncSession。
"""

# ruff: noqa: ARG001  test 函数 ctx / kwargs 是与生产签名一致的占位

from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.ai.agents.tools import (
    SENSITIVE_INPUT_BLOCKLIST,
    SHARED_AGENT_CODE,
    STANDARD_VIEW_TYPES,
    AiToolMeta,
    ToolRegistry,
    ToolRegistryError,
    ai_tool,
    compute_available_tools,
)


@pytest.fixture(autouse=True)
def reset_registry() -> AsyncIterator[None]:
    """每个测试前后重置 Registry 单例，避免跨测试污染"""
    ToolRegistry.reset()
    yield
    ToolRegistry.reset()


# ============ AiToolMeta 字段 ============


class TestAiToolMeta:
    def test_required_fields(self) -> None:
        meta = AiToolMeta(
            name="user.create",
            agent="user_mgmt",
            summary="Create user",
            required_perms=("system:user:add",),
            risk="high",
        )
        assert meta.name == "user.create"
        assert meta.risk == "high"

    def test_default_values(self) -> None:
        """普通 tool 默认值不影响（聚合字段全 falsy）"""
        meta = AiToolMeta(
            name="user.create",
            agent="user_mgmt",
            summary="x",
            required_perms=("p",),
            risk="low",
        )
        assert meta.readonly is False
        assert meta.allowed_filters == ()
        assert meta.allowed_group_by == ()
        assert meta.max_groups == 20
        assert meta.sensitive_input == ()
        assert meta.dry_run_supported is False
        assert meta.hitl_always is False

    def test_frozen(self) -> None:
        """frozen dataclass 保证运行时不可变（FrozenInstanceError 是 AttributeError 子类）"""
        meta = AiToolMeta(
            name="x.y",
            agent="a",
            summary="s",
            required_perms=("p",),
            risk="low",
        )
        with pytest.raises(FrozenInstanceError):
            meta.name = "mutated"  # type: ignore[misc]


# ============ ToolRegistry 单例 ============


class TestToolRegistrySingleton:
    def test_get_returns_same_instance(self) -> None:
        r1 = ToolRegistry.get()
        r2 = ToolRegistry.get()
        assert r1 is r2

    def test_reset_clears_singleton(self) -> None:
        r1 = ToolRegistry.get()
        ToolRegistry.reset()
        r2 = ToolRegistry.get()
        assert r1 is not r2


# ============ register / get / all / by_agent ============


def _make_meta(
    name: str = "user.create",
    agent: str = "user_mgmt",
    perms: tuple[str, ...] = ("system:user:add",),
    *,
    default_enabled: bool = True,
) -> AiToolMeta:
    return AiToolMeta(
        name=name,
        agent=agent,
        summary=f"tool {name}",
        required_perms=perms,
        risk="low",
        default_enabled=default_enabled,
    )


async def _noop_fn(ctx: Any, **kwargs: Any) -> str:
    return "ok"


class TestToolRegistryOps:
    def test_register_and_get(self) -> None:
        registry = ToolRegistry.get()
        meta = _make_meta()
        registry.register(meta, _noop_fn)

        found = registry.find("user.create")
        assert found is not None
        assert found.meta is meta
        assert found.fn is _noop_fn
        assert "user.create" in registry
        assert "missing.tool" not in registry
        assert len(registry) == 1

    def test_register_duplicate_raises(self) -> None:
        registry = ToolRegistry.get()
        registry.register(_make_meta(), _noop_fn)
        with pytest.raises(ToolRegistryError, match="Tool name conflict"):
            registry.register(_make_meta(), _noop_fn)

    def test_get_unknown_returns_none(self) -> None:
        assert ToolRegistry.get().find("missing") is None

    def test_all_returns_list(self) -> None:
        registry = ToolRegistry.get()
        registry.register(_make_meta("a.x"), _noop_fn)
        registry.register(_make_meta("a.y"), _noop_fn)
        assert len(registry.all()) == 2

    def test_by_agent(self) -> None:
        registry = ToolRegistry.get()
        registry.register(_make_meta("u.create", agent="user_mgmt"), _noop_fn)
        registry.register(_make_meta("u.delete", agent="user_mgmt"), _noop_fn)
        registry.register(_make_meta("r.bind", agent="role_mgmt"), _noop_fn)

        user_tools = registry.by_agent("user_mgmt")
        assert len(user_tools) == 2
        assert all(t.meta.agent == "user_mgmt" for t in user_tools)

        assert len(registry.by_agent("missing")) == 0

    def test_set_dry_run_fn_unknown_raises(self) -> None:
        registry = ToolRegistry.get()
        with pytest.raises(ToolRegistryError, match="Cannot set dry_run_fn"):
            registry.set_dry_run_fn("missing", _noop_fn)

    def test_set_dry_run_fn_ok(self) -> None:
        registry = ToolRegistry.get()
        registry.register(_make_meta(), _noop_fn)
        registry.set_dry_run_fn("user.create", _noop_fn)
        found = registry.find("user.create")
        assert found is not None
        assert found.dry_run_fn is _noop_fn


# ============ compute_available_tools ============


class TestComputeAvailableTools:
    def test_perm_filter(self) -> None:
        """spec §5.4: required_perms ⊆ user.perms"""
        registry = ToolRegistry.get()
        registry.register(_make_meta("u.create", perms=("system:user:add",)), _noop_fn)
        registry.register(
            _make_meta("u.delete", perms=("system:user:delete",)), _noop_fn
        )

        # 用户只有 add 权限
        result = compute_available_tools({"system:user:add"}, "user_mgmt")
        assert len(result) == 1
        assert result[0].meta.name == "u.create"

        # 超管有全部权限
        result = compute_available_tools(
            {"system:user:add", "system:user:delete"}, "user_mgmt"
        )
        assert len(result) == 2

    def test_agent_filter(self) -> None:
        """不同 Agent 的 tool 不串"""
        registry = ToolRegistry.get()
        registry.register(_make_meta("u.x", agent="user_mgmt"), _noop_fn)
        registry.register(_make_meta("r.x", agent="role_mgmt"), _noop_fn)

        result = compute_available_tools(set(), "user_mgmt")
        # required_perms=() (默认 _make_meta 给了 ("system:user:add",))
        # 用户 perms=set() 时空集，所以这里返回 0
        # 用 perms 兼容的场景再测
        result = compute_available_tools({"system:user:add"}, "user_mgmt")
        assert len(result) == 1
        assert result[0].meta.name == "u.x"


# ============ v1.5+ SR-17: default_enabled + ai:enabled_tools 白名单 ============


class TestComputeAvailableToolsDefaultEnabled:
    """spec §5.4 SR-17: default_enabled=False 时需在 ai:enabled_tools 白名单才可见"""

    def test_default_enabled_true_visible(self) -> None:
        """default_enabled=True（默认）→ 可见（perms 满足时）"""
        registry = ToolRegistry.get()
        registry.register(_make_meta("u.default_on", default_enabled=True), _noop_fn)

        result = compute_available_tools({"system:user:add"}, "user_mgmt")
        names = [t.meta.name for t in result]
        assert "u.default_on" in names

    def test_default_enabled_false_hidden_without_extra(self) -> None:
        """default_enabled=False + enabled_extra=None（兼容旧调用）→ 仍可见

        None 表示不做 default_enabled 过滤（向后兼容旧测试 / 旧调用方）。
        生产路径由 chat_service.create_agent 预解析 enabled_extra=[] 后传入，
        那时 default_enabled=False 才真正生效。
        """
        registry = ToolRegistry.get()
        registry.register(_make_meta("u.default_off", default_enabled=False), _noop_fn)

        # enabled_extra=None → 不做过滤（向后兼容）
        result = compute_available_tools({"system:user:add"}, "user_mgmt")
        names = [t.meta.name for t in result]
        assert "u.default_off" in names

    def test_default_enabled_false_hidden_with_empty_extra(self) -> None:
        """default_enabled=False + enabled_extra=[] → 不可见"""
        registry = ToolRegistry.get()
        registry.register(_make_meta("u.off_hidden", default_enabled=False), _noop_fn)

        result = compute_available_tools(
            {"system:user:add"}, "user_mgmt", enabled_extra=[]
        )
        names = [t.meta.name for t in result]
        assert "u.off_hidden" not in names

    def test_default_enabled_false_visible_when_in_extra(self) -> None:
        """default_enabled=False + enabled_extra 含该 tool name → 可见"""
        registry = ToolRegistry.get()
        registry.register(
            _make_meta("u.off_whitelisted", default_enabled=False), _noop_fn
        )

        result = compute_available_tools(
            {"system:user:add"},
            "user_mgmt",
            enabled_extra=["u.off_whitelisted"],
        )
        names = [t.meta.name for t in result]
        assert "u.off_whitelisted" in names

    def test_default_enabled_false_extra_does_not_bypass_perms(self) -> None:
        """default_enabled=False + 在 extra 中 + perms 不足 → 仍不可见

        extra 白名单不能绕过 perm 检查（spec §5.4 双维度 AND 关系）。
        """
        registry = ToolRegistry.get()
        registry.register(
            _make_meta(
                "u.off_strict_perm",
                perms=("system:user:delete",),  # 用户没这个 perm
                default_enabled=False,
            ),
            _noop_fn,
        )

        result = compute_available_tools(
            {"system:user:add"},  # 用户只有 add，没 delete
            "user_mgmt",
            enabled_extra=["u.off_strict_perm"],
        )
        names = [t.meta.name for t in result]
        assert "u.off_strict_perm" not in names

    def test_mixed_default_enabled_only_off_filtered(self) -> None:
        """混合场景：3 个 tool，2 个 default=True 1 个 default=False → 仅 False 被过滤"""
        registry = ToolRegistry.get()
        registry.register(_make_meta("u.on1", default_enabled=True), _noop_fn)
        registry.register(_make_meta("u.on2", default_enabled=True), _noop_fn)
        registry.register(_make_meta("u.off1", default_enabled=False), _noop_fn)

        result = compute_available_tools(
            {"system:user:add"}, "user_mgmt", enabled_extra=[]
        )
        names = sorted(t.meta.name for t in result)
        assert names == ["u.on1", "u.on2"]


# ============ @ai_tool 装饰器 ============


class TestAiToolDecorator:
    def test_decorator_registers(self) -> None:
        @ai_tool(
            AiToolMeta(
                name="test.decorator_registers",
                agent="user_mgmt",
                summary="x",
                required_perms=("p1",),
                risk="low",
            )
        )
        async def my_tool(ctx: Any) -> str:
            return "ok"

        # 装饰器返回原函数
        assert callable(my_tool)

        registry = ToolRegistry.get()
        assert "test.decorator_registers" in registry
        found = registry.find("test.decorator_registers")
        assert found is not None
        assert found.fn is my_tool

    def test_decorator_rejects_name_without_dot(self) -> None:
        with pytest.raises(ToolRegistryError, match="dot-separated"):
            ai_tool(
                AiToolMeta(
                    name="no_dot",
                    agent="user_mgmt",
                    summary="x",
                    required_perms=("p",),
                    risk="low",
                )
            )

    def test_decorator_rejects_empty_summary(self) -> None:
        with pytest.raises(ToolRegistryError, match="summary required"):
            ai_tool(
                AiToolMeta(
                    name="x.y",
                    agent="user_mgmt",
                    summary="",
                    required_perms=("p",),
                    risk="low",
                )
            )

    def test_decorator_rejects_long_summary(self) -> None:
        long_summary = "x" * 101
        with pytest.raises(ToolRegistryError, match="100 Unicode chars"):
            ai_tool(
                AiToolMeta(
                    name="x.y",
                    agent="user_mgmt",
                    summary=long_summary,
                    required_perms=("p",),
                    risk="low",
                )
            )

    def test_decorator_rejects_empty_perms_non_shared(self) -> None:
        with pytest.raises(ToolRegistryError, match="required_perms required"):
            ai_tool(
                AiToolMeta(
                    name="x.y",
                    agent="user_mgmt",
                    summary="x",
                    required_perms=(),
                    risk="low",
                )
            )

    def test_decorator_allows_empty_perms_for_shared(self) -> None:
        """spec §16.4: file.parse agent='shared' required_perms=() 合法"""

        @ai_tool(
            AiToolMeta(
                name="shared.ping",
                agent=SHARED_AGENT_CODE,
                summary="x",
                required_perms=(),
                risk="low",
            )
        )
        async def ping(ctx: Any) -> str:
            return "pong"

        assert "shared.ping" in ToolRegistry.get()

    def test_decorator_finds_dry_run_fn(self) -> None:
        """spec §5.1: dry_run_fn 在 _resolve_dry_run_fns 时查找（不是装饰器执行期）

        业务方文件 _dry_run_<tool> 可能定义在 @ai_tool 之后，装饰器执行期
        sys.modules[fn.__module__] 还找不到。延迟到 _resolve_dry_run_fns
        （validate_on_startup 调）时所有模块已加载，查找可靠。
        """

        # 在本测试模块内定义 _dry_run_<tool_dot_to_underscore>
        async def _dry_run_test_decor_dry_run(ctx: Any, **kw: Any) -> dict:
            return {"affected_count": 1}

        # 注入到本模块 globals
        globals()["_dry_run_test_decor_dry_run"] = _dry_run_test_decor_dry_run

        try:

            @ai_tool(
                AiToolMeta(
                    name="test_decor.dry_run",
                    agent="user_mgmt",
                    summary="x",
                    required_perms=("p",),
                    risk="high",
                    dry_run_supported=True,
                )
            )
            async def my_tool(ctx: Any) -> str:
                return "ok"

            registry = ToolRegistry.get()
            found = registry.find("test_decor.dry_run")
            assert found is not None
            # 装饰器执行期不查找（dry_run_fn 为 None）
            assert found.dry_run_fn is None
            # _resolve_dry_run_fns 后查找成功
            registry._resolve_dry_run_fns()
            assert found.dry_run_fn is _dry_run_test_decor_dry_run
        finally:
            globals().pop("_dry_run_test_decor_dry_run", None)


# ============ 启动校验 validate_on_startup ============


class TestValidateOnStartup:
    async def test_empty_registry_skips(self) -> None:
        """空 Registry 跳过校验，避免业务 tool 还没写时启动报错"""
        mock_db = AsyncMock()
        await ToolRegistry.get().validate_on_startup(mock_db)
        # 不调 db.execute
        mock_db.execute.assert_not_called()

    async def test_missing_agent_raises(self) -> None:
        registry = ToolRegistry.get()
        registry.register(_make_meta(agent="missing_agent"), _noop_fn)

        # mock db: ai_agent 表无 missing_agent
        mock_db = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = ["user_mgmt", "role_mgmt"]  # 已有 agent
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        mock_db.execute = AsyncMock(return_value=result_mock)

        # 第一次查 ai_agent → 抛错
        with pytest.raises(ToolRegistryError, match="unknown agent codes"):
            await registry.validate_on_startup(mock_db)

    async def test_missing_perm_raises(self) -> None:
        registry = ToolRegistry.get()
        registry.register(_make_meta(perms=("missing:perm",)), _noop_fn)

        # mock db: ai_agent 有 user_mgmt，但 sys_menu 无 missing:perm
        mock_db = MagicMock()

        # 第一次查 AiAgent.code（有 user_mgmt）
        scalars_mock_1 = MagicMock()
        scalars_mock_1.all.return_value = ["user_mgmt"]
        result_1 = MagicMock()
        result_1.scalars.return_value = scalars_mock_1

        # 第二次查 SysMenu.permission（不含 missing:perm）
        scalars_mock_2 = MagicMock()
        scalars_mock_2.all.return_value = ["system:user:add"]
        result_2 = MagicMock()
        result_2.scalars.return_value = scalars_mock_2

        mock_db.execute = AsyncMock(side_effect=[result_1, result_2])

        with pytest.raises(ToolRegistryError, match="unknown permission codes"):
            await registry.validate_on_startup(mock_db)

    async def test_missing_dry_run_fn_raises(self) -> None:
        """dry_run_supported=True 但没 _dry_run_<tool> → 启动报错"""
        registry = ToolRegistry.get()
        registry.register(
            AiToolMeta(
                name="x.needs_dry_run",
                agent="user_mgmt",
                summary="x",
                required_perms=("system:user:add",),
                risk="high",
                dry_run_supported=True,
            ),
            _noop_fn,
            # 不设 dry_run_fn
        )

        # mock db：agent / perm 都存在
        mock_db = MagicMock()
        scalars_mock_1 = MagicMock()
        scalars_mock_1.all.return_value = ["user_mgmt"]
        result_1 = MagicMock()
        result_1.scalars.return_value = scalars_mock_1

        scalars_mock_2 = MagicMock()
        scalars_mock_2.all.return_value = ["system:user:add"]
        result_2 = MagicMock()
        result_2.scalars.return_value = scalars_mock_2

        mock_db.execute = AsyncMock(side_effect=[result_1, result_2])

        with pytest.raises(ToolRegistryError, match="_dry_run_<tool>"):
            await registry.validate_on_startup(mock_db)


# ============ 常量校验 ============


class TestConstants:
    def test_shared_agent_code(self) -> None:
        assert SHARED_AGENT_CODE == "shared"

    def test_sensitive_input_blocklist_contains_key_fields(self) -> None:
        """spec §7.2: 关键字段必须命中黑名单"""
        required = {"password", "password_hash", "api_key", "secret", "token"}
        assert required <= set(SENSITIVE_INPUT_BLOCKLIST)


# ============ v1.6+ SR-13: result_view 启动校验 ============


class TestValidateResultViewOnStartup:
    """spec 2026-07-16-tool-result-view-design.md §2.4：result_view 启动校验。"""

    async def test_invalid_result_view_rejected(self) -> None:
        """meta.result_view 不在 STANDARD_VIEW_TYPES 时启动校验失败。"""
        meta = AiToolMeta(
            name="test.invalid_view",
            agent="user_mgmt",
            summary="t",
            required_perms=("system:user:add",),
            risk="low",
        )
        # 绕过 frozen dataclass 校验，直接改 result_view
        object.__setattr__(meta, "result_view", "invalid_view_type")
        assert "invalid_view_type" not in STANDARD_VIEW_TYPES

        registry = ToolRegistry.get()
        registry.register(meta, _noop_fn)

        # mock db：agent / perm 都存在，绕过 step 1-3，让 step 5 触发
        mock_db = MagicMock()
        scalars_mock_1 = MagicMock()
        scalars_mock_1.all.return_value = ["user_mgmt"]
        result_1 = MagicMock()
        result_1.scalars.return_value = scalars_mock_1

        scalars_mock_2 = MagicMock()
        scalars_mock_2.all.return_value = ["system:user:add"]
        result_2 = MagicMock()
        result_2.scalars.return_value = scalars_mock_2

        mock_db.execute = AsyncMock(side_effect=[result_1, result_2])

        with pytest.raises(ToolRegistryError, match="invalid_view_type"):
            await registry.validate_on_startup(mock_db)

    def test_default_result_view_is_plain_json(self) -> None:
        """未声明 result_view 时默认 'plain_json'。"""
        meta = AiToolMeta(
            name="test.default",
            agent="user_mgmt",
            summary="t",
            required_perms=("p",),
            risk="low",
        )
        assert meta.result_view == "plain_json"
        assert "plain_json" in STANDARD_VIEW_TYPES

    def test_standard_view_types_has_five_members(self) -> None:
        assert STANDARD_VIEW_TYPES == frozenset(
            {"rows_affected", "data_list", "stats_chart", "detail_card", "plain_json"}
        )
