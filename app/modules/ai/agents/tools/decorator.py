"""@ai_tool 装饰器 — 双重身份：注册到 Registry + 标记业务函数

声明 AI 工具元数据，并把函数注册到全局 ToolRegistry。

用法：
    @ai_tool(AiToolMeta(
        name="user.create",
        agent="user_mgmt",
        summary="Create a new user account",
        required_perms=("system:user:add",),
        risk="high",
        sensitive_input=("password",),  # 声明但不进函数签名
    ))
    async def create_user(ctx: AiToolContext, username: str, dept_id: int):
        ...

dry-run 函数查找约定：
    name='user.create' → 同模块必须定义 async def _dry_run_user_create(ctx, ...)
    装饰器执行期不查找（业务方文件顺序不可控），validate_on_startup 时统一查找
    （在 ToolRegistry._resolve_dry_run_fns 内）。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from .meta import SHARED_AGENT_CODE, AiToolMeta
from .registry import ToolRegistry, ToolRegistryError


def ai_tool(
    meta: AiToolMeta,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """装饰器：注册 tool 到 Registry

    工作流：
      1. 校验 meta 字段（必填非空 / summary 长度）
      2. 注册到 ToolRegistry（重名抛 ToolRegistryError）
      3. 返回原函数，包装由 PydanticAI 适配层完成

    为什么不立即包装为 PydanticAI tool：
      - PydanticAI 包装需要 ``AiToolContext``
      - Registry 注册 ≠ PydanticAI tool 创建，两者职责分离
      - 通过 ``build_pydantic_ai_tools(agent_code, perms)`` 动态生成包装
    """

    # ============ 启动期校验 meta 字段（lint 兜底） ============
    if not meta.name or "." not in meta.name:
        raise ToolRegistryError(
            f"AiToolMeta.name must be non-empty and dot-separated (e.g. 'user.create'), "
            f"got: {meta.name!r}"
        )
    if not meta.agent:
        raise ToolRegistryError("AiToolMeta.agent must be non-empty")
    if not meta.summary:
        raise ToolRegistryError(f"AiToolMeta.summary required for tool {meta.name!r}")
    if len(meta.summary) > 100:
        raise ToolRegistryError(
            f"AiToolMeta.summary for {meta.name!r} exceeds 100 Unicode chars "
            f"(got {len(meta.summary)}); shorten for LLM schema friendliness"
        )
    if not meta.required_perms and meta.agent != "shared":
        # shared Agent 允许 required_perms=()，表示任何登录用户都可调用。
        raise ToolRegistryError(
            f"AiToolMeta.required_perms required for tool {meta.name!r} "
            f"(only {SHARED_AGENT_CODE!r} agent may have empty perms)"
        )

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        # Keep the immutable declaration on the function as well as in the
        # process registry.  Static/audit tests can inspect built-ins without
        # depending on mutable Registry singleton state or import order.
        fn.__ai_tool_meta__ = meta  # type: ignore[attr-defined]

        # 注册到 Registry
        registry = ToolRegistry.get()
        registry.register(meta, fn)

        # dry_run_fn 延迟到启动校验阶段查找。
        # — 装饰器执行期业务方文件可能还没解析到 _dry_run_<tool>（函数定义在
        # 装饰器之后），此时 sys.modules[fn.__module__] 上找不到。startup 时
        # 所有模块已加载完毕，查找可靠。
        return fn

    return decorator
