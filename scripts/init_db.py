# ruff: noqa: T201

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.id_generator import next_id
from app.core.security import get_password_hash
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

system_id = next_id()

init_menus = [
    Menu(
        parent_id="0",
        menu_name="首页",
        menu_type="C",
        icon="mdi:monitor-dashboard",
        icon_type="1",
        component="layout.base$view.home",
        route_name="home",
        route_path="/home",
        i18n_key="route.home",
        order=0,
        status="2",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
        create_time="2025-12-30T12:05:39.344705",
    ),
    Menu(
        parent_id="0",
        menu_name="系统管理",
        menu_type="M",
        icon="carbon:cloud-service-management",
        icon_type="1",
        component="layout.base",
        route_name="system",
        route_path="/system",
        i18n_key="route.system",
        order=99,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=system_id,
    ),
    Menu(
        parent_id=system_id,
        menu_name="用户管理",
        menu_type="C",
        icon="ic:round-manage-accounts",
        icon_type="1",
        component="view.system_user",
        route_name="system_user",
        route_path="/system/user",
        i18n_key="route.system_user",
        order=1,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=system_id,
        menu_name="角色管理",
        menu_type="C",
        icon="mdi:account-group",
        icon_type="1",
        component="view.system_role",
        route_name="system_role",
        route_path="/system/role",
        i18n_key="route.system_role",
        order=2,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=system_id,
        menu_name="菜单管理",
        menu_type="C",
        icon="mdi:file-tree",
        icon_type="1",
        component="view.system_menu",
        route_name="system_menu",
        route_path="/system/menu",
        i18n_key="route.system_menu",
        order=3,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
]


async def init_db():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    password = input("初始化密码 [默认: hohu123456]: ").strip() or "hohu123456"

    async with async_session() as db:
        # 创建初始菜单
        db.add_all(init_menus)

        # 创建超级管理员角色
        admin_role = Role(role_name="超级管理员", role_code="R_SUPER", status="1")
        db.add(admin_role)

        # 创建初始管理员用户
        admin_user = User(
            username="admin",
            nickname="管理员",
            hashed_password=get_password_hash(password),
            status="1",
        )
        admin_user.roles = [admin_role]
        db.add(admin_user)

        await db.commit()
        print("✅ 数据库初始化完成：管理员账号 admin 密码 " + password)


if __name__ == "__main__":
    asyncio.run(init_db())
