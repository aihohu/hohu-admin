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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.id_generator import next_id
from app.modules.system.models.menu import Menu

# 菜单定义：每条记录用 parent_route 替代 parent_id，运行时自动解析。
# route_name 作为唯一标识，已存在则跳过。
#
# parent_route 规则:
#   "0"  — 顶级目录
#   其他 — 对应父菜单的 route_name
#
# 新增菜单只需在末尾追加，不要修改已有条目。

MENU_DEFINITIONS = [
    # ============ 首页 ============
    {
        "route_name": "home",
        "parent_route": "0",
        "menu_name": "首页",
        "menu_type": "C",
        "icon": "carbon:home",
        "icon_type": "1",
        "component": "layout.base$view.home",
        "layout": "base",
        "page": "home",
        "route_path": "/home",
        "i18n_key": "route.home",
        "order": 0,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ============ 系统管理 ============
    {
        "route_name": "system",
        "parent_route": "0",
        "menu_name": "系统管理",
        "menu_type": "M",
        "icon": "carbon:cloud-service-management",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/system",
        "i18n_key": "route.system",
        "order": 99,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_dept",
        "parent_route": "system",
        "menu_name": "部门管理",
        "menu_type": "C",
        "icon": "carbon:user-multiple",
        "icon_type": "1",
        "component": "view.system_dept",
        "page": "system_dept",
        "route_path": "/system/dept",
        "i18n_key": "route.system_dept",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_user",
        "parent_route": "system",
        "menu_name": "用户管理",
        "menu_type": "C",
        "icon": "carbon:user",
        "icon_type": "1",
        "component": "view.system_user",
        "page": "system_user",
        "route_path": "/system/user",
        "i18n_key": "route.system_user",
        "order": 2,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_role",
        "parent_route": "system",
        "menu_name": "角色管理",
        "menu_type": "C",
        "icon": "carbon:user-role",
        "icon_type": "1",
        "component": "view.system_role",
        "page": "system_role",
        "route_path": "/system/role",
        "i18n_key": "route.system_role",
        "order": 3,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_menu",
        "parent_route": "system",
        "menu_name": "菜单管理",
        "menu_type": "C",
        "icon": "carbon:menu",
        "icon_type": "1",
        "component": "view.system_menu",
        "page": "system_menu",
        "route_path": "/system/menu",
        "i18n_key": "route.system_menu",
        "order": 4,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_dict",
        "parent_route": "system",
        "menu_name": "字典管理",
        "menu_type": "C",
        "icon": "fluent-mdl2:dictionary",
        "icon_type": "1",
        "component": "view.system_dict",
        "page": "system_dict",
        "route_path": "/system/dict",
        "i18n_key": "route.system_dict",
        "order": 5,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_dict_data",
        "parent_route": "system",
        "menu_name": "字典数据",
        "menu_type": "C",
        "icon": "fluent-mdl2:dictionary",
        "icon_type": "1",
        "component": "view.system_dict_data",
        "page": "system_dict_data",
        "route_path": "/system/dict/data",
        "i18n_key": "route.system_dict_data",
        "order": 6,
        "status": "1",
        "hide_in_menu": True,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_file",
        "parent_route": "system",
        "menu_name": "文件管理",
        "menu_type": "C",
        "icon": "carbon:cloud-upload",
        "icon_type": "1",
        "component": "view.system_file",
        "page": "system_file",
        "route_path": "/system/file",
        "i18n_key": "route.system_file",
        "order": 7,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_job",
        "parent_route": "system",
        "menu_name": "定时任务",
        "menu_type": "C",
        "icon": "carbon:timer",
        "icon_type": "1",
        "component": "view.system_job",
        "page": "system_job",
        "route_path": "/system/job",
        "i18n_key": "route.system_job",
        "order": 8,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_job-log",
        "parent_route": "system",
        "menu_name": "任务日志",
        "menu_type": "C",
        "icon": "carbon:document-tasks",
        "icon_type": "1",
        "component": "view.system_job-log",
        "page": "system_job-log",
        "route_path": "/system/job-log",
        "i18n_key": "route.system_job-log",
        "order": 9,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ============ AI 模块 ============
    {
        "route_name": "ai",
        "parent_route": "0",
        "menu_name": "AI 助手",
        "menu_type": "M",
        "icon": "carbon:chat-bot",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/ai",
        "i18n_key": "route.ai",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "ai_chat",
        "parent_route": "ai",
        "menu_name": "AI 对话",
        "menu_type": "C",
        "icon": "carbon:chat",
        "icon_type": "1",
        "component": "view.ai_chat",
        "page": "ai_chat",
        "route_path": "/ai/chat",
        "i18n_key": "route.ai_chat",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "ai_provider",
        "parent_route": "ai",
        "menu_name": "模型管理",
        "menu_type": "C",
        "icon": "carbon:settings-adjust",
        "icon_type": "1",
        "component": "view.ai_provider",
        "page": "ai_provider",
        "route_path": "/ai/provider",
        "i18n_key": "route.ai_provider",
        "order": 2,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
]


async def sync_menus():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        # 1. 查询所有已存在的 route_name
        result = await db.execute(select(Menu.route_name))
        existing = set(result.scalars().all())

        # 2. 过滤出需要新增的菜单
        new_defs = [d for d in MENU_DEFINITIONS if d["route_name"] not in existing]

        if not new_defs:
            print("All menus already exist, nothing to sync.")
            await engine.dispose()
            return

        # 3. 查询需要用到的父菜单 ID（已存在的 + 本轮即将插入的）
        #    先处理已存在数据库中的父菜单
        parent_routes_needed = {
            d["parent_route"] for d in new_defs if d["parent_route"] != "0"
        }
        result = await db.execute(
            select(Menu.menu_id, Menu.route_name).where(
                Menu.route_name.in_(parent_routes_needed)
            )
        )
        route_to_id = {name: mid for mid, name in result.all()}

        # 4. 按依赖顺序插入（先目录后子菜单）
        #    多轮处理确保父子依赖都能解析
        inserted = {}
        remaining = list(new_defs)

        for _ in range(3):  # 最多 3 层嵌套，足够了
            next_round = []
            for d in remaining:
                parent_route = d["parent_route"]

                # 解析 parent_id
                if parent_route == "0":
                    parent_id = 0
                elif parent_route in route_to_id:
                    parent_id = route_to_id[parent_route]
                elif parent_route in inserted:
                    parent_id = inserted[parent_route]
                else:
                    next_round.append(d)
                    continue

                menu_id = next_id()
                menu = Menu(
                    menu_id=menu_id,
                    parent_id=parent_id,
                    route_name=d["route_name"],
                    menu_name=d["menu_name"],
                    menu_type=d["menu_type"],
                    icon=d.get("icon"),
                    icon_type=d.get("icon_type"),
                    component=d.get("component"),
                    layout=d.get("layout"),
                    page=d.get("page"),
                    route_path=d.get("route_path"),
                    i18n_key=d.get("i18n_key"),
                    order=d.get("order", 0),
                    status=d.get("status", "1"),
                    hide_in_menu=d.get("hide_in_menu", False),
                    keep_alive=d.get("keep_alive", False),
                    constant=d.get("constant", False),
                    multi_tab=d.get("multi_tab", False),
                )
                db.add(menu)
                inserted[d["route_name"]] = menu_id
                print(f"  + [{d['menu_type']}] {d['menu_name']} ({d['route_name']})")

            remaining = next_round
            if not remaining:
                break

        if remaining:
            print(f"Warning: {len(remaining)} menus skipped (parent not found):")
            for d in remaining:
                print(f"  - {d['menu_name']} (parent: {d['parent_route']})")

        await db.commit()
        print(
            f"\nSynced {len(inserted)} new menus. Total menus in DB: {len(existing) + len(inserted)}"
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(sync_menus())
