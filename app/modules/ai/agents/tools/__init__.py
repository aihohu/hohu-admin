"""AI Tool Gateway — 装饰器 + Registry + Meta

集中导出工具注册、元数据、执行结果和包装入口。

典型用法：
    from app.modules.ai.agents.tools import AiToolMeta, ai_tool

    @ai_tool(AiToolMeta(
        name="user.create",
        agent="user_mgmt",
        summary="Create a new user account",
        required_perms=("system:user:add",),
        risk="high",
    ))
    async def create_user(ctx, username: str, dept_id: int):
        ...

启动校验：
    from app.modules.ai.agents.tools import ToolRegistry
    await ToolRegistry.get().validate_on_startup(db)
"""

from .decorator import ai_tool
from .meta import (
    SENSITIVE_INPUT_BLOCKLIST,
    SHARED_AGENT_CODE,
    STANDARD_VIEW_TYPES,
    AiToolMeta,
)
from .registry import (
    RegisteredTool,
    ToolRegistry,
    ToolRegistryError,
    compute_available_tools,
)
from .stats_validator import (
    validate_field_in_whitelist,
    validate_filters_in_whitelist,
    validate_group_by_in_whitelist,
)

__all__ = [
    "BUILTIN_TOOL_MODULES",
    "AiToolMeta",
    "RegisteredTool",
    "SENSITIVE_INPUT_BLOCKLIST",
    "SHARED_AGENT_CODE",
    "STANDARD_VIEW_TYPES",
    "ToolRegistry",
    "ToolRegistryError",
    "ai_tool",
    "compute_available_tools",
    "load_builtin_tools",
    "validate_field_in_whitelist",
    "validate_filters_in_whitelist",
    "validate_group_by_in_whitelist",
]


# 注意：pydantic_ai_wrapper 故意不在顶部 import，避免循环：
#   context.py → tools/__init__.py → pydantic_ai_wrapper → context.py
# 使用时显式 from app.modules.ai.agents.tools.pydantic_ai_wrapper import ...


# ============ 启动扫描：触发各业务模块 @ai_tool 装饰器注册到 Registry ============
# 启动时导入带 @ai_tool 的模块，填充全局 ToolRegistry。
# 由 main.py lifespan 或测试 fixture 显式调用，避免在 import 期触发循环依赖。

BUILTIN_TOOL_MODULES = (
    "app.modules.system.ai_tools.analytics",
    "app.modules.system.ai_tools.role",
    "app.modules.system.ai_tools.dept",
    "app.modules.system.ai_tools.user_assignment",
    "app.modules.system.ai_tools.user_management",
    "app.modules.system.ai_tools.user_transfer",
    "app.modules.job.ai_tools",
    "app.modules.ai.agents.tools.file_tools",
)


def load_builtin_tools() -> None:
    """触发内置工具注册。

    显式调用（不在 import 期触发），避免 system/ai_tools → context → User →
    db.base 的 import 链与 tools/__init__.py 自身形成循环。

    用法：
        # main.py lifespan
        from app.modules.ai.agents.tools import load_builtin_tools
        load_builtin_tools()
        await ToolRegistry.get().validate_on_startup(db)

        # 测试 fixture
        load_builtin_tools()
    """
    from importlib import import_module  # noqa: PLC0415  延迟 import 避免循环

    modules = [import_module(module_name) for module_name in BUILTIN_TOOL_MODULES]

    # import_module() 对已缓存模块不会再次执行装饰器。测试隔离、热重载或其他
    # 显式 reset 场景下，Registry 可能已清空而模块仍在 sys.modules；此时从函数
    # 上的不可变声明恢复注册，避免注册结果依赖导入顺序。正常启动路径中 existing
    # 与 candidate 是同一函数，因此保持幂等；同名异源仍 fail-fast。
    registry = ToolRegistry.get()
    for module in modules:
        for candidate in vars(module).values():
            meta = getattr(candidate, "__ai_tool_meta__", None)
            if not isinstance(meta, AiToolMeta):
                continue
            if getattr(candidate, "__module__", None) != module.__name__:
                continue

            existing = registry.find(meta.name)
            if existing is None:
                registry.register(meta, candidate)
                continue
            if existing.fn is not candidate or existing.meta != meta:
                raise ToolRegistryError(
                    f"Tool name conflict while loading built-ins: {meta.name!r} "
                    f"already registered at {existing.module_path}, cannot restore "
                    f"{candidate.__module__}.{candidate.__qualname__}"
                )
