"""ToolRegistry — 启动时反射构建，运行时按 Agent + perms 过滤

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §3 / §5.1 / §5.4 / §12.4。

生命周期：
  1. 装饰器执行期（import time）：ToolRegistry.register(meta, fn)
  2. FastAPI lifespan 启动：ToolRegistry.get().validate_on_startup(db)
     - resolve_dry_run_fns（spec §5.1，所有模块加载完后查找）
     - perms_must_exist_in_menu（spec §12.4）
     - agent_must_exist_in_db（spec §10.1）
     - dry_run_fn_must_be_set（dry_run_supported=True 时强制）
  3. /ai/chat 请求期：compute_available_tools(user, agent) 按 perms 过滤
"""

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.models.agent import AiAgent
from app.modules.system.models.menu import Menu as SysMenu

from .meta import AiToolMeta


class ToolRegistryError(Exception):
    """Registry 启动校验失败时抛出。"""


@dataclass
class RegisteredTool:
    """ToolRegistry 注册项：meta + 业务函数 + dry_run 函数（可选）。

    PydanticAI 包装（RunContext[ChatDeps] → AiToolContext 拆包）在 Phase 1.2b 实施，
    届时本类会加 `pydantic_ai_tool: ToolDefinition` 字段。
    """

    meta: AiToolMeta
    fn: Callable[..., Awaitable[Any]]
    """业务方原函数，签名是 async def fn(ctx: AiToolContext, **args)"""

    dry_run_fn: Callable[..., Awaitable[Any]] | None = None
    """可选 dry_run 函数（命名约定 _dry_run_<tool>），dry_run_supported=True 时必填"""

    module_path: str = ""
    """业务函数所在模块路径，便于调试 / lint 报错定位"""


class ToolRegistry:
    """Tool 注册中心，模块级单例。

    用法：
        from app.modules.ai.agents.tools import ToolRegistry
        registry = ToolRegistry.get()

        # 启动时校验（lifespan）
        await registry.validate_on_startup(db)

        # 运行时查询
        tool = registry.get("user.create")
        all_tools = registry.all()
        agent_tools = registry.by_agent("user_mgmt")
    """

    _instance: "ToolRegistry | None" = None

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    @classmethod
    def get(cls) -> "ToolRegistry":
        """获取单例（首次访问时创建）"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置单例，仅供测试用"""
        cls._instance = None

    def register(self, meta: AiToolMeta, fn: Callable[..., Awaitable[Any]]) -> None:
        """注册一个 tool。重名直接抛错（启动失败比运行时漂移安全）"""
        if meta.name in self._tools:
            existing = self._tools[meta.name]
            raise ToolRegistryError(
                f"Tool name conflict: {meta.name!r} "
                f"already registered at {existing.module_path}, "
                f"cannot register again at {fn.__module__}.{fn.__qualname__}"
            )
        self._tools[meta.name] = RegisteredTool(
            meta=meta,
            fn=fn,
            module_path=f"{fn.__module__}.{fn.__qualname__}",
        )

    def set_dry_run_fn(
        self, name: str, dry_run_fn: Callable[..., Awaitable[Any]]
    ) -> None:
        """装饰器反射查到 _dry_run_<tool> 后，回填到 RegisteredTool"""
        if name not in self._tools:
            raise ToolRegistryError(
                f"Cannot set dry_run_fn for unknown tool {name!r}; register tool first"
            )
        self._tools[name].dry_run_fn = dry_run_fn

    def find(self, name: str) -> RegisteredTool | None:
        """按 name 查 tool，不存在返回 None。与单例方法 get() 区分"""
        return self._tools.get(name)

    def all(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def by_agent(self, agent_code: str) -> list[RegisteredTool]:
        """spec §5.4: 按 Agent code 过滤 tool"""
        return [t for t in self._tools.values() if t.meta.agent == agent_code]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    # ============ dry_run_fn 解析（spec §5.1） ============

    def _resolve_dry_run_fns(self) -> None:
        """启动时统一查找每个 tool 的 _dry_run_<tool> 函数（spec §5.1）

        命名约定：name='user.create' → 同模块必须定义 async def _dry_run_user_create。
        用 sys.modules[fn.__module__] + getattr 查找，找不到则跳过（dry_run_supported
        校验在 validate_on_startup 第 4 步统一报错）。
        """
        for name, tool in self._tools.items():
            if tool.dry_run_fn is not None:
                continue  # 已有（极少见，避免重复查找覆盖）
            module_name = tool.fn.__module__
            if not module_name or module_name not in sys.modules:
                continue
            module = sys.modules[module_name]
            fn_name = f"_dry_run_{name.replace('.', '_')}"
            dry_run_fn = getattr(module, fn_name, None)
            if dry_run_fn is not None:
                tool.dry_run_fn = dry_run_fn

    async def validate_on_startup(self, db: AsyncSession) -> None:
        """启动校验（spec §5.1 / §12.4）。

        校验项：
          0. resolve_dry_run_fns: 装饰器执行期未查找的 dry_run_fn 此时统一解析
          1. agent_must_exist_in_db: 每个 tool 的 meta.agent 必须在 ai_agent 表存在
          2. perms_must_exist_in_menu: 每个 required_perms 必须在 sys_menu 表存在
          3. dry_run_fn_must_be_set: dry_run_supported=True 的 tool 必须有 dry_run_fn

        任一校验失败抛 ToolRegistryError，FastAPI lifespan 应捕获并拒绝启动。
        """
        if not self._tools:
            # 空注册表（业务 tool 还没写），跳过校验避免误报
            return

        # 0. 解析 dry_run_fn（spec §5.1）
        # 装饰器执行期业务方文件可能还没解析完（_dry_run_<tool> 定义在 @ai_tool
        # 之后），此时 sys.modules[fn.__module__] 找不到。startup 时所有模块都
        # 已加载完，可靠查找。
        self._resolve_dry_run_fns()

        # 1. 收集所有 agent code + perms
        agent_codes = {t.meta.agent for t in self._tools.values()}
        perms: set[str] = set()
        for t in self._tools.values():
            perms.update(t.meta.required_perms)

        # 2. 查 ai_agent 表确认 code 存在
        existing_agents_result = await db.execute(select(AiAgent.code))
        existing_agents = set(existing_agents_result.scalars().all())
        missing_agents = agent_codes - existing_agents
        if missing_agents:
            raise ToolRegistryError(
                f"Tool registry references unknown agent codes: {sorted(missing_agents)}. "
                "Add them via scripts/seed_ai_agents.py or sys_menu AI Agent 管理页."
            )

        # 3. 查 sys_menu 表确认 perms 存在（permission 字段非空）
        existing_perms_result = await db.execute(
            select(SysMenu.permission).where(SysMenu.permission.is_not(None))
        )
        existing_perms = set(existing_perms_result.scalars().all())
        missing_perms = perms - existing_perms
        if missing_perms:
            raise ToolRegistryError(
                f"Tool registry references unknown permission codes: {sorted(missing_perms)}. "
                "Add them via scripts/sync_menus.py."
            )

        # 4. dry_run_supported=True 必须有 dry_run_fn
        missing_dry_run = [
            t.meta.name
            for t in self._tools.values()
            if t.meta.dry_run_supported and t.dry_run_fn is None
        ]
        if missing_dry_run:
            raise ToolRegistryError(
                f"Tools with dry_run_supported=True must define _dry_run_<tool> "
                f"in the same module: {missing_dry_run}. "
                "Naming convention: name='user.create' → _dry_run_user_create."
            )


def compute_available_tools(
    user_perms: set[str],
    agent_code: str,
) -> list[RegisteredTool]:
    """spec §5.4: 运行时按 Agent + 用户权限码过滤可见 tool

    - Tool 可见性只看 required_perms ⊆ user_perms
    - Agent 可见性在 compute_available_agents（spec §5.4）单独处理
    - 超管 user_perms={'*'} 时单独走 is_super_admin 路径（调用方负责）

    返回值不包含 sensitive_input 字段信息（LLM schema 看不到这些字段，§7.2）。
    """
    registry = ToolRegistry.get()
    return [
        t
        for t in registry.by_agent(agent_code)
        if set(t.meta.required_perms) <= user_perms
    ]
