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
    # ============ AI 助手 ============
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
    # ============ 权限管理 ============
    {
        "route_name": "auth",
        "parent_route": "0",
        "menu_name": "权限管理",
        "menu_type": "M",
        "icon": "carbon:security",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/auth",
        "i18n_key": "route.auth",
        "order": 98,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_dept",
        "parent_route": "auth",
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
        "parent_route": "auth",
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
    # ---- 用户管理按钮权限 ----
    {
        "key": "system_user_list",
        "parent_route": "system_user",
        "menu_name": "查询",
        "menu_type": "F",
        "permission": "system:user:list",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_add",
        "parent_route": "system_user",
        "menu_name": "新增",
        "menu_type": "F",
        "permission": "system:user:add",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_edit",
        "parent_route": "system_user",
        "menu_name": "修改",
        "menu_type": "F",
        "permission": "system:user:edit",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_delete",
        "parent_route": "system_user",
        "menu_name": "删除",
        "menu_type": "F",
        "permission": "system:user:delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_batch-delete",
        "parent_route": "system_user",
        "menu_name": "批量删除",
        "menu_type": "F",
        "permission": "system:user:batch-delete",
        "route_path": "",
        "status": "1",
    },
    {
        "key": "system_user_reset-password",
        "parent_route": "system_user",
        "menu_name": "重置密码",
        "menu_type": "F",
        "permission": "system:user:reset-password",
        "route_path": "",
        "status": "1",
    },
    {
        "route_name": "system_role",
        "parent_route": "auth",
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
        "parent_route": "auth",
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
        "order": 1,
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
        "order": 2,
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
        "order": 3,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ---- 系统设置 ----
    {
        "route_name": "system_config",
        "parent_route": "system",
        "menu_name": "系统设置",
        "menu_type": "C",
        "icon": "carbon:settings",
        "icon_type": "1",
        "component": "view.system_config",
        "page": "system_config",
        "route_path": "/system/config",
        "i18n_key": "route.system_config",
        "order": 4,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    # ============ 任务中心 ============
    {
        "route_name": "task",
        "parent_route": "0",
        "menu_name": "任务中心",
        "menu_type": "M",
        "icon": "carbon:task",
        "icon_type": "1",
        "component": "layout.base",
        "layout": "base",
        "route_path": "/task",
        "i18n_key": "route.task",
        "order": 100,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_job",
        "parent_route": "task",
        "menu_name": "定时任务",
        "menu_type": "C",
        "icon": "carbon:timer",
        "icon_type": "1",
        "component": "view.system_job",
        "page": "system_job",
        "route_path": "/system/job",
        "i18n_key": "route.system_job",
        "order": 1,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
    {
        "route_name": "system_job-log",
        "parent_route": "task",
        "menu_name": "任务日志",
        "menu_type": "C",
        "icon": "carbon:document-tasks",
        "icon_type": "1",
        "component": "view.system_job-log",
        "page": "system_job-log",
        "route_path": "/system/job-log",
        "i18n_key": "route.system_job-log",
        "order": 2,
        "status": "1",
        "hide_in_menu": False,
        "keep_alive": False,
        "constant": False,
        "multi_tab": False,
    },
]


def _get_def_key(d: dict) -> str:
    """获取菜单定义的唯一标识：F 类型用 key，其他用 route_name"""
    return d.get("key") or d["route_name"]


async def sync_menus():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        # 1. 查询所有已存在的 route_name 和 permission
        result = await db.execute(select(Menu.route_name))
        existing_routes = set(result.scalars().all())

        result2 = await db.execute(select(Menu.permission))
        existing_perms = {p for p in result2.scalars().all() if p is not None}

        # 2. 过滤出需要新增的菜单（F 类型按 permission 去重，其他按 route_name 去重）
        new_defs = []
        for d in MENU_DEFINITIONS:
            if d["menu_type"] == "F":
                perm = d.get("permission")
                if perm and perm in existing_perms:
                    continue
            else:
                if d["route_name"] in existing_routes:
                    continue
            new_defs.append(d)

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
                is_button = d["menu_type"] == "F"
                menu = Menu(
                    menu_id=menu_id,
                    parent_id=parent_id,
                    route_name=None if is_button else d["route_name"],
                    menu_name=d["menu_name"],
                    menu_type=d["menu_type"],
                    permission=d.get("permission"),
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
                def_key = _get_def_key(d)
                inserted[parent_route if is_button else d["route_name"]] = menu_id
                print(f"  + [{d['menu_type']}] {d['menu_name']} ({def_key})")

            remaining = next_round
            if not remaining:
                break

        if remaining:
            print(f"Warning: {len(remaining)} menus skipped (parent not found):")
            for d in remaining:
                print(f"  - {d['menu_name']} (parent: {d['parent_route']})")

        await db.commit()
        print(
            f"\nSynced {len(inserted)} new menus. Total menus in DB: {len(existing_routes) + len(inserted)}"
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(sync_menus())
