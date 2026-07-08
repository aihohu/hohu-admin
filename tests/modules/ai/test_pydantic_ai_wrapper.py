"""PydanticAI 包装层单元测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5.1 / §6.3 / §5.4。
"""

# ruff: noqa: ARG001, PLC0415  test 函数 ctx / kwargs 占位 + 测试内局部 import

import inspect
from typing import Any

import pytest
from pydantic_ai import RunContext, Tool

from app.modules.ai.agents.tools import (
    AiToolMeta,
    RegisteredTool,
    ToolRegistry,
    ai_tool,
)
from app.modules.ai.agents.tools.pydantic_ai_wrapper import (
    build_pydantic_ai_tools,
    wrap_tool_for_pydantic_ai,
)


@pytest.fixture(autouse=True)
def reset_registry():
    ToolRegistry.reset()
    yield
    ToolRegistry.reset()


def _register_sample_tool(
    name: str = "sample.tool",
    agent: str = "user_mgmt",
    perms: tuple[str, ...] = ("system:user:list",),
) -> "RegisteredTool":
    """注册一个示例 tool 并返回 RegisteredTool"""

    @ai_tool(
        AiToolMeta(
            name=name,
            agent=agent,
            summary=f"sample {name}",
            required_perms=perms,
            risk="low",
        )
    )
    async def sample_fn(ctx: Any, foo: str, bar: int = 0) -> dict:
        return {"foo": foo, "bar": bar}

    return ToolRegistry.get().find(name)  # type: ignore[return-value]


# ============ wrap_tool_for_pydantic_ai ============


class TestWrapTool:
    def test_returns_pydantic_ai_tool_instance(self) -> None:
        registered = _register_sample_tool()
        tool = wrap_tool_for_pydantic_ai(registered)
        assert isinstance(tool, Tool)

    def test_tool_name_and_description_from_meta(self) -> None:
        registered = _register_sample_tool(name="user.lookup")
        tool = wrap_tool_for_pydantic_ai(registered)
        assert tool.name == "user.lookup"
        assert tool.description == "sample user.lookup"

    def test_wrapper_signature_replaces_ctx_type(self) -> None:
        """wrapper 第一个参数 ctx 类型必须是 RunContext[ChatDeps]"""
        from typing import get_origin

        from app.modules.ai.core.context import ChatDeps

        registered = _register_sample_tool()
        tool = wrap_tool_for_pydantic_ai(registered)

        wrapper_fn = tool.function
        sig = inspect.signature(wrapper_fn)
        first_param = next(iter(sig.parameters.values()))
        # annotation 是 RunContext[ChatDeps]（已参数化的 generic alias）
        assert get_origin(first_param.annotation) is RunContext
        assert first_param.annotation.__args__ == (ChatDeps,)

    def test_wrapper_preserves_business_params(self) -> None:
        """spec §5.1: 业务参数（除 ctx）保持原样，进 LLM schema"""
        registered = _register_sample_tool()
        tool = wrap_tool_for_pydantic_ai(registered)

        wrapper_fn = tool.function
        sig = inspect.signature(wrapper_fn)
        params = list(sig.parameters.values())
        # 第一个 ctx，第二第三是 foo / bar
        assert params[1].name == "foo"
        assert params[2].name == "bar"
        assert params[2].default == 0

    def test_wrapper_annotations_has_run_context(self) -> None:
        """PydanticAI 用 typing.get_type_hints 读 annotations 推断 schema"""
        from typing import get_origin

        from app.modules.ai.core.context import ChatDeps

        registered = _register_sample_tool()
        tool = wrap_tool_for_pydantic_ai(registered)

        wrapper_fn = tool.function
        annotations = wrapper_fn.__annotations__
        # ctx 注解应该是 RunContext[ChatDeps]
        first_param_name = next(
            iter(inspect.signature(registered.fn).parameters.keys())
        )
        annotation = annotations[first_param_name]
        assert get_origin(annotation) is RunContext
        assert annotation.__args__ == (ChatDeps,)


# ============ build_pydantic_ai_tools ============


class TestBuildPydanticAiTools:
    def test_returns_empty_for_no_matching_tools(self) -> None:
        """Agent code 匹配但 perms 不满足 → 空"""
        _register_sample_tool()
        tools = build_pydantic_ai_tools(set(), "user_mgmt")
        assert tools == []

    def test_returns_empty_for_unknown_agent(self) -> None:
        """Agent code 不存在 → 空"""
        _register_sample_tool()
        tools = build_pydantic_ai_tools({"system:user:list"}, "missing_agent")
        assert tools == []

    def test_filters_by_agent_code(self) -> None:
        """spec §5.4: 不同 Agent 的 tool 不串"""
        _register_sample_tool(name="user.lookup", agent="user_mgmt")
        _register_sample_tool(name="role.list", agent="role_mgmt")

        user_tools = build_pydantic_ai_tools(
            {"system:user:list", "system:role:list"}, "user_mgmt"
        )
        assert len(user_tools) == 1
        assert user_tools[0].name == "user.lookup"

    def test_filters_by_perms(self) -> None:
        """spec §5.4: required_perms ⊆ user.perms"""
        _register_sample_tool(name="user.create", perms=("system:user:add",))
        _register_sample_tool(name="user.delete", perms=("system:user:delete",))

        # 用户只有 add 权限
        tools = build_pydantic_ai_tools({"system:user:add"}, "user_mgmt")
        assert len(tools) == 1
        assert tools[0].name == "user.create"

    def test_full_perm_set_returns_all(self) -> None:
        """超管有全部权限 → 返回全部"""
        _register_sample_tool(name="user.create", perms=("system:user:add",))
        _register_sample_tool(name="user.delete", perms=("system:user:delete",))

        tools = build_pydantic_ai_tools(
            {"system:user:add", "system:user:delete"}, "user_mgmt"
        )
        assert len(tools) == 2
