# ruff: noqa: T201

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.constants.constants import ADMIN_ROLE_CODE, SUPER_ADMIN_ROLE_CODE
from app.core.config import settings
from app.core.id_generator import next_id
from app.core.security import get_password_hash
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

system_id = next_id()
ai_id = next_id()

init_menus = [
    Menu(
        parent_id=0,
        menu_name="首页",
        menu_type="C",
        icon="carbon:home",
        icon_type="1",
        component="layout.base$view.home",
        layout="base",
        page="home",
        route_name="home",
        route_path="/home",
        i18n_key="route.home",
        order=0,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=0,
        menu_name="系统管理",
        menu_type="M",
        icon="carbon:cloud-service-management",
        icon_type="1",
        component="layout.base",
        layout="base",
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
        menu_name="部门管理",
        menu_type="C",
        icon="carbon:user-multiple",
        icon_type="1",
        component="view.system_dept",
        page="system_dept",
        route_name="system_dept",
        route_path="/system/dept",
        i18n_key="route.system_dept",
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
        menu_name="用户管理",
        menu_type="C",
        icon="carbon:user",
        icon_type="1",
        component="view.system_user",
        page="system_user",
        route_name="system_user",
        route_path="/system/user",
        i18n_key="route.system_user",
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
        menu_name="角色管理",
        menu_type="C",
        icon="carbon:user-role",
        icon_type="1",
        component="view.system_role",
        page="system_role",
        route_name="system_role",
        route_path="/system/role",
        i18n_key="route.system_role",
        order=3,
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
        icon="carbon:menu",
        icon_type="1",
        component="view.system_menu",
        page="system_menu",
        route_name="system_menu",
        route_path="/system/menu",
        i18n_key="route.system_menu",
        order=4,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=system_id,
        menu_name="字典管理",
        menu_type="C",
        icon="fluent-mdl2:dictionary",
        icon_type="1",
        component="view.system_dict",
        page="system_dict",
        route_name="system_dict",
        route_path="/system/dict",
        i18n_key="route.system_dict",
        order=5,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=system_id,
        menu_name="字典数据",
        menu_type="C",
        icon="fluent-mdl2:dictionary",
        icon_type="1",
        component="view.system_dict_data",
        page="system_dict_data",
        route_name="system_dict_data",
        route_path="/system/dict/data",
        i18n_key="route.system_dict_data",
        order=6,
        status="1",
        hide_in_menu=True,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=system_id,
        menu_name="文件管理",
        menu_type="C",
        icon="carbon:cloud-upload",
        icon_type="1",
        component="view.system_file",
        page="system_file",
        route_name="system_file",
        route_path="/system/file",
        i18n_key="route.system_file",
        order=7,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=system_id,
        menu_name="定时任务",
        menu_type="C",
        icon="carbon:timer",
        icon_type="1",
        component="view.system_job",
        page="system_job",
        route_name="system_job",
        route_path="/system/job",
        i18n_key="route.system_job",
        order=8,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=system_id,
        menu_name="任务日志",
        menu_type="C",
        icon="carbon:document-tasks",
        icon_type="1",
        component="view.system_job-log",
        page="system_job-log",
        route_name="system_job-log",
        route_path="/system/job-log",
        i18n_key="route.system_job-log",
        order=9,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    # ============ AI 模块 ============
    Menu(
        parent_id=0,
        menu_name="AI 助手",
        menu_type="M",
        icon="carbon:chat-bot",
        icon_type="1",
        component="layout.base",
        layout="base",
        route_name="ai",
        route_path="/ai",
        i18n_key="route.ai",
        order=1,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=ai_id,
    ),
    Menu(
        parent_id=ai_id,
        menu_name="AI 对话",
        menu_type="C",
        icon="carbon:chat",
        icon_type="1",
        component="view.ai_chat",
        page="ai_chat",
        route_name="ai_chat",
        route_path="/ai/chat",
        i18n_key="route.ai_chat",
        order=1,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=ai_id,
        menu_name="模型管理",
        menu_type="C",
        icon="carbon:settings-adjust",
        icon_type="1",
        component="view.ai_provider",
        page="ai_provider",
        route_name="ai_provider",
        route_path="/ai/provider",
        i18n_key="route.ai_provider",
        order=2,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=next_id(),
    ),
]

SEED_TABLES = [
    "sys_user_role",
    "sys_role_menu",
    "sys_user",
    "sys_role",
    "sys_menu",
]


async def check_data_exists(db: AsyncSession) -> bool:
    result = await db.execute(text("SELECT EXISTS(SELECT 1 FROM sys_user LIMIT 1)"))
    return result.scalar()


async def clear_seed_data(db: AsyncSession):
    for table in SEED_TABLES:
        await db.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
    await db.commit()
    print("✅ 已清空所有种子数据。")


async def init_db():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        if await check_data_exists(db):
            print("⚠️ 检测到数据库中已存在数据。")
            choice = input("是否清空后重新初始化? (y/n): ").lower()  # noqa: ASYNC250
            if choice != "y":
                print("⏭️ 跳过数据初始化。")
                return
            await clear_seed_data(db)

        password = input("初始化密码 [默认: hohu123456]: ").strip() or "hohu123456"  # noqa: ASYNC250

        # 创建初始菜单
        db.add_all(init_menus)

        # 创建超级管理员角色
        admin_role = Role(
            role_name="超级管理员", role_code=SUPER_ADMIN_ROLE_CODE, status="1"
        )
        db.add(admin_role)

        # 创建初始管理员用户
        admin_user = User(
            user_name="admin",
            nickname=ADMIN_ROLE_CODE,
            hashed_password=get_password_hash(password),
            status="1",
        )
        admin_user.roles = [admin_role]
        db.add(admin_user)

        await db.commit()
        print("✅ 数据库初始化完成：管理员账号 admin 密码 " + password)


if __name__ == "__main__":
    asyncio.run(init_db())
