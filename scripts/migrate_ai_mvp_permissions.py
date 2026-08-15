# ruff: noqa: T201
"""幂等迁移 AI MVP 权限、配置与 R_SUPER Agent 绑定。"""

import asyncio
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.config import settings
from app.core.id_generator import next_id
from app.db.base import role_menus
from app.modules.ai.constants import (
    AI_AGENT_EDIT_PERMISSION,
    AI_CHAT_USE_PERMISSION,
    AI_FILE_PARSE_PERMISSION,
    PUBLISHED_AGENT_CODES,
)
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.models.config import Config
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role


@dataclass(frozen=True)
class PermissionMigrationResult:
    menu_created: bool
    roles_granted: int
    super_agent_bindings_created: int = 0


async def _ensure_ai_agent_parent(db: AsyncSession) -> Menu:
    parent = await db.scalar(select(Menu).where(Menu.route_name == "ai_agent"))
    if parent is not None:
        return parent
    ai_root = await db.scalar(select(Menu).where(Menu.route_name == "ai"))
    if ai_root is None:
        raise RuntimeError("ai parent menu not found; run menu seed first")
    parent = Menu(
        menu_id=next_id(),
        parent_id=ai_root.menu_id,
        menu_name="AI 助手管理",
        menu_type="C",
        icon="carbon:bot",
        icon_type="1",
        component="view.ai_agent",
        page="ai_agent",
        route_name="ai_agent",
        route_path="/ai/agent",
        i18n_key="route.ai_agent",
        order=3,
        status=STATUS_ENABLED,
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
    )
    db.add(parent)
    await db.flush()
    return parent


async def _ensure_permission_menu(
    db: AsyncSession,
    *,
    permission: str,
    parent: Menu,
    name: str,
) -> tuple[Menu, bool]:
    menu = await db.scalar(select(Menu).where(Menu.permission == permission))
    if menu is not None:
        return menu, False
    menu = Menu(
        menu_id=next_id(),
        parent_id=parent.menu_id,
        menu_name=name,
        menu_type="F",
        permission=permission,
        status=STATUS_ENABLED,
    )
    db.add(menu)
    await db.flush()
    return menu, True


async def _grant_menu(
    db: AsyncSession,
    *,
    menu_id: int,
    role_ids: set[int],
) -> int:
    if not role_ids:
        return 0
    existing = set(
        (
            await db.execute(
                select(role_menus.c.role_id).where(
                    role_menus.c.role_id.in_(role_ids),
                    role_menus.c.menu_id == menu_id,
                )
            )
        ).scalars()
    )
    missing = role_ids - existing
    if missing:
        await db.execute(
            insert(role_menus),
            [{"role_id": role_id, "menu_id": menu_id} for role_id in sorted(missing)],
        )
    return len(missing)


async def _bind_published_agents_to_super(
    db: AsyncSession,
    *,
    super_role_ids: set[int],
) -> int:
    if not super_role_ids:
        return 0
    agent_ids = set(
        (
            await db.execute(
                select(AiAgent.agent_id).where(AiAgent.code.in_(PUBLISHED_AGENT_CODES))
            )
        ).scalars()
    )
    if not agent_ids:
        return 0
    rows = (
        await db.execute(
            select(RoleAiAgent).where(
                RoleAiAgent.role_id.in_(super_role_ids),
                RoleAiAgent.agent_id.in_(agent_ids),
            )
        )
    ).scalars()
    existing_pairs: set[tuple[int, int]] = set()
    for row in rows:
        existing_pairs.add((row.role_id, row.agent_id))
        # 现有 enabled 是部署方显式状态；upgrade 只补缺失行，不把手工禁用翻回。
    created = 0
    for role_id in super_role_ids:
        for agent_id in agent_ids:
            if (role_id, agent_id) in existing_pairs:
                continue
            db.add(
                RoleAiAgent(
                    role_id=role_id,
                    agent_id=agent_id,
                    enabled=True,
                )
            )
            created += 1
    await db.flush()
    return created


async def _ensure_upgrade_enabled_tools_config(db: AsyncSession) -> None:
    existing = await db.scalar(
        select(Config).where(Config.config_key == "ai:enabled_tools")
    )
    if existing is not None:
        return
    db.add(
        Config(
            config_id=next_id(),
            config_name="AI 额外启用工具",
            config_key="ai:enabled_tools",
            config_value="[]",
            config_type="text",
            config_group="ai",
            status=STATUS_ENABLED,
            is_public=False,
            remark="upgrade 默认不自动启用 file.parse",
        )
    )
    await db.flush()


async def migrate_ai_mvp_permissions(
    db: AsyncSession,
) -> PermissionMigrationResult:
    """执行可事务回滚的 upgrade 数据迁移；调用方负责 commit。"""
    chat_parent = await db.scalar(select(Menu).where(Menu.route_name == "ai_chat"))
    if chat_parent is None:
        raise RuntimeError("ai_chat parent menu not found; run menu seed first")
    agent_parent = await _ensure_ai_agent_parent(db)

    chat_menu, chat_created = await _ensure_permission_menu(
        db,
        permission=AI_CHAT_USE_PERMISSION,
        parent=chat_parent,
        name="使用 AI 对话",
    )
    file_menu, _ = await _ensure_permission_menu(
        db,
        permission=AI_FILE_PARSE_PERMISSION,
        parent=chat_parent,
        name="解析聊天文件",
    )
    agent_list_menu, _ = await _ensure_permission_menu(
        db,
        permission="ai:agent:list",
        parent=agent_parent,
        name="查询",
    )
    agent_edit_menu, _ = await _ensure_permission_menu(
        db,
        permission=AI_AGENT_EDIT_PERMISSION,
        parent=agent_parent,
        name="修改",
    )

    super_role_ids = set(
        (
            await db.execute(
                select(Role.role_id).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalars()
    )
    chat_role_ids = set(super_role_ids)
    chat_role_ids.update(
        (
            await db.execute(
                select(RoleAiAgent.role_id)
                .join(AiAgent, AiAgent.agent_id == RoleAiAgent.agent_id)
                .where(AiAgent.code != "shared")
                .distinct()
            )
        ).scalars()
    )
    roles_granted = await _grant_menu(
        db,
        menu_id=chat_menu.menu_id,
        role_ids=chat_role_ids,
    )
    for menu in (file_menu, agent_list_menu, agent_edit_menu):
        await _grant_menu(
            db,
            menu_id=menu.menu_id,
            role_ids=super_role_ids,
        )

    bindings_created = await _bind_published_agents_to_super(
        db,
        super_role_ids=super_role_ids,
    )
    await _ensure_upgrade_enabled_tools_config(db)
    return PermissionMigrationResult(
        menu_created=chat_created,
        roles_granted=roles_granted,
        super_agent_bindings_created=bindings_created,
    )


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with async_session() as db:
            async with db.begin():
                result = await migrate_ai_mvp_permissions(db)
        print(
            "AI MVP permission migration complete: "
            f"menu_created={result.menu_created}, "
            f"roles_granted={result.roles_granted}, "
            f"super_agent_bindings_created={result.super_agent_bindings_created}"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
