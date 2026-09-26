# ruff: noqa: T201

"""
菜单增量同步工具

按 route_name 去重，只添加数据库中不存在的菜单，已存在则跳过。
适用于版本升级时新增菜单，可安全重复执行。

Usage:
    cd hohu-admin
    python scripts/sync_menus.py
"""

import asyncio

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.id_generator import next_id
from app.core.tenant import DEFAULT_TENANT_ID
from app.db.base import role_menus
from app.modules.system.constants import (
    PLATFORM_ONLY_TENANT_ROUTE_NAMES,
)
from app.modules.system.hosted_menu_seed import HOSTED_MENU_DEFINITIONS

# 菜单定义：每条记录用 parent_route 替代 parent_id，运行时自动解析。
# route_name 作为唯一标识，已存在则跳过。
#
# parent_route 规则:
#   "0"  — 顶级目录
#   其他 — 对应父菜单的 route_name
#
# 新增菜单只需在末尾追加，不要修改已有条目。
from app.modules.system.menu_seed import (
    MENU_DEFINITIONS,
    menu_values,
)
from app.modules.system.menu_seed import build_initial_menus as build_initial_menus
from app.modules.system.models.menu import Menu

_MENU_PARTITIONS = {
    "auth": {
        "system_dept": 1,
        "system_user": 2,
        "system_role": 3,
        "system_menu": 4,
    },
    "system": {
        "system_dict": 1,
        "system_dict_data": 2,
        "system_file": 3,
        "system_setting": 4,
        "system_config": 5,
        "system_data-scope-demo": 8,
        "system_monitor": 10,
    },
    "task": {
        "system_job": 1,
        "system_job-log": 2,
    },
}


async def _reconcile_menu_partitions(
    db: AsyncSession,
    *,
    partitions: dict[str, dict[str, int]] = _MENU_PARTITIONS,
) -> bool:
    """Restore known menu pages to their established domain roots and order."""
    route_names = set(partitions)
    route_names.update(
        child_route
        for child_routes in partitions.values()
        for child_route in child_routes
    )
    # tenant 0 only: route_names repeat across tenants, and a foreign tenant's
    # row would reparent tenant-0 children onto a cross-tenant parent (FK error).
    result = await db.execute(
        select(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID,
            Menu.route_name.in_(route_names),
        )
    )
    menus = {menu.route_name: menu for menu in result.scalars().all()}
    changed = False
    for root_route, child_routes in partitions.items():
        root = menus.get(root_route)
        if root is None:
            continue
        for child_route, order in child_routes.items():
            child = menus.get(child_route)
            if child is None:
                continue
            if child.parent_id != root.menu_id or child.order != order:
                child.parent_id = root.menu_id
                child.order = order
                changed = True

    return changed


async def _retire_platform_only_tenant_menus(db: AsyncSession) -> int:
    """Delete obsolete tenant menu roots and their button permissions."""
    roots = (
        (
            await db.execute(
                select(Menu.menu_id).where(
                    Menu.tenant_id == DEFAULT_TENANT_ID,
                    Menu.route_name.in_(PLATFORM_ONLY_TENANT_ROUTE_NAMES),
                )
            )
        )
        .scalars()
        .all()
    )
    if not roots:
        return 0

    children = (
        (
            await db.execute(
                select(Menu.menu_id).where(
                    Menu.tenant_id == DEFAULT_TENANT_ID,
                    Menu.parent_id.in_(roots),
                )
            )
        )
        .scalars()
        .all()
    )
    retired_ids = [*children, *roots]
    await db.execute(
        delete(role_menus).where(
            role_menus.c.tenant_id == DEFAULT_TENANT_ID,
            role_menus.c.menu_id.in_(retired_ids),
        )
    )
    if children:
        await db.execute(
            delete(Menu).where(
                Menu.tenant_id == DEFAULT_TENANT_ID,
                Menu.menu_id.in_(children),
            )
        )
    await db.execute(
        delete(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID,
            Menu.menu_id.in_(roots),
        )
    )
    return len(retired_ids)


async def _refactor_ai_first_home(db: AsyncSession) -> str | None:
    """One-shot AI-first home refactor (idempotent, tenant 0 only).

    Runs only while the legacy home menu exists: remove it, promote ai_chat
    to the top-level "AI 助手" page, rename the ai group to "AI 管理", and
    add the dashboard menu under system. Fresh init_db databases have no
    home menu, making this a no-op.
    """
    home_menu = (
        (
            await db.execute(
                select(Menu).where(
                    Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == "home"
                )
            )
        )
        .scalars()
        .first()
    )
    if home_menu is None:
        return None

    await db.execute(
        delete(role_menus).where(role_menus.c.menu_id == home_menu.menu_id)
    )
    await db.delete(home_menu)

    ai_chat_menu = (
        (
            await db.execute(
                select(Menu).where(
                    Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == "ai_chat"
                )
            )
        )
        .scalars()
        .first()
    )
    if ai_chat_menu is not None:
        ai_chat_menu.parent_id = 0
        ai_chat_menu.menu_name = "AI 助手"
        ai_chat_menu.icon = "carbon:chat-bot"
        ai_chat_menu.component = "layout.base$view.ai_chat"
        ai_chat_menu.layout = "base"
        ai_chat_menu.order = 0

    ai_group = (
        (
            await db.execute(
                select(Menu).where(
                    Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == "ai"
                )
            )
        )
        .scalars()
        .first()
    )
    if ai_group is not None:
        ai_group.menu_name = "AI 管理"

    dashboard_exists = (
        (
            await db.execute(
                select(Menu.route_name).where(
                    Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == "dashboard"
                )
            )
        )
        .scalars()
        .first()
    )
    if dashboard_exists is None:
        system_menu = (
            (
                await db.execute(
                    select(Menu).where(
                        Menu.tenant_id == DEFAULT_TENANT_ID, Menu.route_name == "system"
                    )
                )
            )
            .scalars()
            .first()
        )
        if system_menu is not None:
            db.add(
                Menu(
                    tenant_id=DEFAULT_TENANT_ID,
                    menu_id=next_id(),
                    parent_id=system_menu.menu_id,
                    **menu_values(
                        next(
                            d
                            for d in MENU_DEFINITIONS
                            if d.get("route_name") == "dashboard"
                        )
                    ),
                )
            )

    await db.flush()
    return "ai-first home refactor applied (home removed, ai_chat promoted, ai renamed, dashboard added)"


async def sync_menus_in_session(
    db: AsyncSession,
    *,
    tenant_id: int = DEFAULT_TENANT_ID,
    hosted: bool = False,
) -> list[Menu]:
    """Add missing built-ins without changing existing grants or committing."""
    if tenant_id == DEFAULT_TENANT_ID:
        await _retire_platform_only_tenant_menus(db)
        await _refactor_ai_first_home(db)
    existing = list(
        (await db.scalars(select(Menu).where(Menu.tenant_id == tenant_id))).all()
    )
    by_route = {m.route_name: m for m in existing if m.route_name}
    by_permission = {m.permission: m for m in existing if m.permission}
    definitions = HOSTED_MENU_DEFINITIONS if hosted else MENU_DEFINITIONS
    for definition in definitions:
        menu = (
            by_permission.get(definition.get("permission"))
            if definition["menu_type"] == "F"
            else by_route.get(definition["route_name"])
        )
        if (
            menu is not None
            and menu.i18n_key is None
            and menu.menu_name == definition["menu_name"]
        ):
            menu.i18n_key = menu_values(definition).get("i18n_key")
    pending = [
        d
        for d in definitions
        if not (
            d.get("permission") in by_permission
            if d["menu_type"] == "F"
            else d["route_name"] in by_route
        )
    ]
    while pending:
        remaining = []
        inserted = []
        for d in pending:
            parent = d["parent_route"]
            if parent != "0" and parent not in by_route:
                remaining.append(d)
                continue
            menu = Menu(
                tenant_id=tenant_id,
                menu_id=next_id(),
                parent_id=None if parent == "0" else by_route[parent].menu_id,
                **menu_values(d),
            )
            db.add(menu)
            # Flush each parent before adding its children (composite FK).
            await db.flush()
            inserted.append(menu)
            if menu.route_name:
                by_route[menu.route_name] = menu
            if menu.permission:
                by_permission[menu.permission] = menu
        if not inserted:
            raise ValueError("menu catalog contains an unresolved parent")
        pending = remaining
    if tenant_id == DEFAULT_TENANT_ID:
        await _reconcile_menu_partitions(db)
    await db.flush()
    return list(
        (await db.scalars(select(Menu).where(Menu.tenant_id == tenant_id))).all()
    )


async def sync_menus():
    engine = create_async_engine(settings.DATABASE_URL)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db, db.begin():
            await sync_menus_in_session(db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(sync_menus())
