"""系统查询工具

Agent 可调用的工具函数，查询 hohu 管理平台的系统数据。
"""

from pydantic_ai import Agent, RunContext
from sqlalchemy import func, select

from app.modules.ai.core.config import ChatDeps
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User


def register_system_tools(agent: Agent):
    """向 Agent 注册系统查询工具"""

    @agent.tool
    async def get_user_stats(ctx: RunContext[ChatDeps]) -> str:
        """查询用户统计信息。"""
        db = ctx.deps.db
        total_stmt = select(func.count()).select_from(User)
        total_result = await db.execute(total_stmt)
        total = total_result.scalar() or 0

        enabled_stmt = select(func.count()).select_from(User).where(User.status == "1")
        enabled_result = await db.execute(enabled_stmt)
        enabled = enabled_result.scalar() or 0

        return f"用户总数: {total}, 启用: {enabled}, 禁用: {total - enabled}"

    @agent.tool
    async def get_system_info(ctx: RunContext[ChatDeps]) -> str:
        """查询系统基本信息（版本、在线用户数等）"""
        db = ctx.deps.db
        user_count = (
            await db.execute(select(func.count()).select_from(User))
        ).scalar() or 0
        role_count = (
            await db.execute(select(func.count()).select_from(Role))
        ).scalar() or 0
        dept_count = (
            await db.execute(select(func.count()).select_from(Dept))
        ).scalar() or 0
        menu_count = (
            await db.execute(select(func.count()).select_from(Menu))
        ).scalar() or 0

        return (
            f"HoHu Admin 管理平台\n"
            f"用户数: {user_count}\n"
            f"角色数: {role_count}\n"
            f"部门数: {dept_count}\n"
            f"菜单数: {menu_count}"
        )
