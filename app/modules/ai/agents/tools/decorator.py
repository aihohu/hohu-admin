"""@ai_tool 装饰器 — 双重身份：注册到 Registry + 标记业务函数

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5.1 / §5.5。

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

dry_run 函数查找约定（spec §5.1）：
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
      3. 返回原函数（不包装，包装在 Phase 1.2b PydanticAI 适配层做）

    为什么不立即包装为 PydanticAI tool：
      - PydanticAI 包装需要 AiToolContext（Phase 1.3 后才定义）
      - Registry 注册 ≠ PydanticAI tool 创建，两者职责分离
      - Phase 1.2b 实施时通过 build_pydantic_ai_tools(agent_code, perms) 动态生成
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
        # shared Agent 允许 required_perms=()（任何登录用户可调，spec §16.4 file.parse）
        raise ToolRegistryError(
            f"AiToolMeta.required_perms required for tool {meta.name!r} "
            f"(only {SHARED_AGENT_CODE!r} agent may have empty perms)"
        )

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        # 注册到 Registry
        registry = ToolRegistry.get()
        registry.register(meta, fn)

        # dry_run_fn 查找延迟到 validate_on_startup（spec §5.1）
        # — 装饰器执行期业务方文件可能还没解析到 _dry_run_<tool>（函数定义在
        # 装饰器之后），此时 sys.modules[fn.__module__] 上找不到。startup 时
        # 所有模块已加载完毕，查找可靠。
        return fn

    return decorator
