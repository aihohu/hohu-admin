# ruff: noqa: T201

import asyncio

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.constants.constants import (
    ADMIN_ROLE_CODE,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
    USER_ROLE_CODE,
)
from app.core.config import settings
from app.core.id_generator import next_id
from app.core.security import get_password_hash
from app.modules.ai.constants import (
    AI_AGENT_EDIT_PERMISSION,
    AI_CHAT_USE_PERMISSION,
    AI_FILE_PARSE_PERMISSION,
    PUBLISHED_AGENT_CODES,
    PUBLISHED_AGENT_TOOL_PERMISSIONS,
)
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.constants import (
    DEPT_MOVE_PERMISSION,
    PHASE3_DESTRUCTIVE_PERMISSIONS,
    USER_ROLE_AUTH_PERMISSION,
)
from app.modules.system.models.config import Config
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from scripts.seed_ai_agents import seed_ai_agents_in_session

system_id = next_id()
ai_id = next_id()
monitor_id = next_id()
# 用户管理菜单和导入、导出按钮共用 menu_id。
# 链路，按钮 parent_id 必须指向 system_user 菜单。模块级避免内联 next_id() 后无法被
# 后续按钮引用。
_system_user_menu_id = next_id()
_system_dept_menu_id = next_id()
_system_role_menu_id = next_id()
_ai_chat_menu_id = next_id()
_ai_agent_menu_id = next_id()

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
        menu_id=_system_dept_menu_id,
    ),
    # Department Tool permissions are explicit even for the fresh super role.
    Menu(
        parent_id=_system_dept_menu_id,
        menu_name="查询",
        menu_type="F",
        permission="system:dept:list",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_dept_menu_id,
        menu_name="新增",
        menu_type="F",
        permission="system:dept:add",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_dept_menu_id,
        menu_name="修改",
        menu_type="F",
        permission="system:dept:edit",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_dept_menu_id,
        menu_name="移动",
        menu_type="F",
        permission=DEPT_MOVE_PERMISSION,
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_dept_menu_id,
        menu_name="删除",
        menu_type="F",
        permission="system:dept:delete",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_dept_menu_id,
        menu_name="批量删除",
        menu_type="F",
        permission="system:dept:batch-delete",
        status=STATUS_ENABLED,
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
        menu_id=_system_user_menu_id,
    ),
    # 初始化用户导入和导出按钮权限。
    Menu(
        parent_id=_system_user_menu_id,
        menu_name="查询",
        menu_type="F",
        permission="system:user:list",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_user_menu_id,
        menu_name="新增",
        menu_type="F",
        permission="system:user:add",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_user_menu_id,
        menu_name="修改",
        menu_type="F",
        permission="system:user:edit",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_user_menu_id,
        menu_name="删除",
        menu_type="F",
        permission="system:user:delete",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_user_menu_id,
        menu_name="重置密码",
        menu_type="F",
        permission="system:user:reset-password",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_user_menu_id,
        menu_name="导入",
        menu_type="F",
        permission="system:user:import",
        status="1",
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_user_menu_id,
        menu_name="导出",
        menu_type="F",
        permission="system:user:export",
        status="1",
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_user_menu_id,
        menu_name="角色授权",
        menu_type="F",
        permission=USER_ROLE_AUTH_PERMISSION,
        status=STATUS_ENABLED,
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
        menu_id=_system_role_menu_id,
    ),
    Menu(
        parent_id=_system_role_menu_id,
        menu_name="查询",
        menu_type="F",
        permission="system:role:list",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_role_menu_id,
        menu_name="新增",
        menu_type="F",
        permission="system:role:add",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_role_menu_id,
        menu_name="修改",
        menu_type="F",
        permission="system:role:edit",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_role_menu_id,
        menu_name="菜单授权",
        menu_type="F",
        permission="system:role:menu-auth",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_role_menu_id,
        menu_name="Agent 授权",
        menu_type="F",
        permission="system:role:ai-agent-auth",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_role_menu_id,
        menu_name="删除",
        menu_type="F",
        permission="system:role:delete",
        status=STATUS_ENABLED,
        menu_id=next_id(),
    ),
    Menu(
        parent_id=_system_role_menu_id,
        menu_name="批量删除",
        menu_type="F",
        permission="system:role:batch-delete",
        status=STATUS_ENABLED,
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
    # ============ 监控管理 ============
    Menu(
        parent_id=system_id,
        menu_name="监控管理",
        menu_type="M",
        icon="carbon:activity",
        icon_type="1",
        component="layout.base",
        layout="base",
        route_name="system_monitor",
        route_path="/system/monitor",
        i18n_key="route.system_monitor",
        order=10,
        status="1",
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        menu_id=monitor_id,
    ),
]

# 操作日志和登录日志的 menu_id（按钮级权限菜单需要引用）
_operation_log_menu_id = next_id()
_login_log_menu_id = next_id()

init_menus.extend(
    [
        Menu(
            parent_id=monitor_id,
            menu_name="操作日志",
            menu_type="C",
            icon="carbon:document",
            icon_type="1",
            component="view.system_operation-log",
            page="system_operation-log",
            route_name="system_operation-log",
            route_path="/system/operation-log",
            i18n_key="route.system_operation-log",
            order=1,
            status="1",
            hide_in_menu=False,
            keep_alive=False,
            constant=False,
            multi_tab=False,
            menu_id=_operation_log_menu_id,
        ),
        Menu(
            parent_id=_operation_log_menu_id,
            menu_name="查询",
            menu_type="F",
            permission="monitor:operation-log:list",
            status="1",
            menu_id=next_id(),
        ),
        Menu(
            parent_id=_operation_log_menu_id,
            menu_name="删除",
            menu_type="F",
            permission="monitor:operation-log:delete",
            status="1",
            menu_id=next_id(),
        ),
        Menu(
            parent_id=_operation_log_menu_id,
            menu_name="清理",
            menu_type="F",
            permission="monitor:operation-log:clean",
            status="1",
            menu_id=next_id(),
        ),
        Menu(
            parent_id=monitor_id,
            menu_name="登录日志",
            menu_type="C",
            icon="carbon:login",
            icon_type="1",
            component="view.system_login-log",
            page="system_login-log",
            route_name="system_login-log",
            route_path="/system/login-log",
            i18n_key="route.system_login-log",
            order=2,
            status="1",
            hide_in_menu=False,
            keep_alive=False,
            constant=False,
            multi_tab=False,
            menu_id=_login_log_menu_id,
        ),
        Menu(
            parent_id=_login_log_menu_id,
            menu_name="查询",
            menu_type="F",
            permission="monitor:login-log:list",
            status="1",
            menu_id=next_id(),
        ),
        Menu(
            parent_id=_login_log_menu_id,
            menu_name="删除",
            menu_type="F",
            permission="monitor:login-log:delete",
            status="1",
            menu_id=next_id(),
        ),
        Menu(
            parent_id=_login_log_menu_id,
            menu_name="清理",
            menu_type="F",
            permission="monitor:login-log:clean",
            status="1",
            menu_id=next_id(),
        ),
    ]
)

init_menus.extend(
    [
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
            menu_id=_ai_chat_menu_id,
        ),
        Menu(
            parent_id=_ai_chat_menu_id,
            menu_name="使用 AI 对话",
            menu_type="F",
            permission=AI_CHAT_USE_PERMISSION,
            status=STATUS_ENABLED,
            menu_id=next_id(),
        ),
        Menu(
            parent_id=_ai_chat_menu_id,
            menu_name="解析聊天文件",
            menu_type="F",
            permission=AI_FILE_PARSE_PERMISSION,
            status=STATUS_ENABLED,
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
        Menu(
            parent_id=ai_id,
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
            menu_id=_ai_agent_menu_id,
        ),
        Menu(
            parent_id=_ai_agent_menu_id,
            menu_name="查询",
            menu_type="F",
            permission="ai:agent:list",
            status=STATUS_ENABLED,
            menu_id=next_id(),
        ),
        Menu(
            parent_id=_ai_agent_menu_id,
            menu_name="修改",
            menu_type="F",
            permission=AI_AGENT_EDIT_PERMISSION,
            status=STATUS_ENABLED,
            menu_id=next_id(),
        ),
        Menu(
            parent_id=ai_id,
            menu_name="AI Trace 查看",
            menu_type="C",
            permission="ai:trace:view",
            icon="carbon:flow-logs-vpc",
            icon_type="1",
            component="view.ai_trace",
            page="ai_trace",
            route_name="ai_trace",
            route_path="/ai/trace",
            i18n_key="route.ai_trace",
            order=4,
            status=STATUS_ENABLED,
            hide_in_menu=False,
            keep_alive=False,
            constant=False,
            multi_tab=False,
            menu_id=next_id(),
        ),
    ]
)


# sys_config 种子：初始化业务必需配置项。
# 已有部署走 sync_menus.py 增量拿菜单按钮，但 sys_config 不归 sync_menus 管，
# 现有部署的 default_password 由部署方在 admin UI 手动配（helper 缺失时抛
# AI_IMPORT_DEFAULT_PASSWORD_NOT_SET，避免静默用错密码）。
def default_password_seed_value(env: str) -> str:
    """开发环境提供可用种子；生产环境必须由部署方显式配置。"""
    return "" if env == "prod" else "Hohu123456"


init_configs = [
    # 导入新用户的默认密码以配置值保存，创建用户时再哈希。
    # is_public=False：未授权访问 /system/config/public 不暴露此键（防敏感配置泄漏）
    Config(
        config_name="导入用户默认密码",
        config_key="auth:default_password",
        config_value=default_password_seed_value(settings.ENV),
        config_type="text",
        config_group="auth",
        status="1",
        is_public=False,
        remark=(
            "【安全提示】批量导入新用户的初始密码（明文存库，导入时哈希）。"
            "上线前请改为强密码并定期轮换；prod 环境禁止保留默认值 Hohu123456。"
            "管理员线下告知用户初始密码，导入接口不返回此值。"
        ),
    ),
    Config(
        config_name="AI 额外启用工具",
        config_key="ai:enabled_tools",
        config_value='["file.parse"]',
        config_type="text",
        config_group="ai",
        status=STATUS_ENABLED,
        is_public=False,
        remark="fresh install 显式启用 file.parse；升级保留部署方既有值",
    ),
]

SEED_TABLES = [
    "sys_user_role",
    "sys_role_menu",
    "sys_user",
    "sys_role",
    "sys_menu",
    "sys_config",
]


def build_init_roles() -> list[Role]:
    """构造与既有角色编码契约一致的 fresh-install 角色种子。"""
    return [
        Role(
            role_name="超级管理员",
            role_code=SUPER_ADMIN_ROLE_CODE,
            status=STATUS_ENABLED,
        ),
        Role(
            role_name="普通用户",
            role_code=USER_ROLE_CODE,
            role_desc="AI user.create 与普通账号使用的后端默认角色",
            data_scope=DATA_SCOPE_SELF,
            status=STATUS_ENABLED,
        ),
    ]


def bind_fresh_role_permissions(admin_role: Role, menus: list[Menu]) -> None:
    """Bind every published Agent permission explicitly for fresh R_SUPER."""
    permissions = {
        AI_CHAT_USE_PERMISSION,
        AI_FILE_PARSE_PERMISSION,
        "ai:agent:list",
        AI_AGENT_EDIT_PERMISSION,
        *PUBLISHED_AGENT_TOOL_PERMISSIONS,
        *PHASE3_DESTRUCTIVE_PERMISSIONS,
    }
    admin_role.menus = [menu for menu in menus if menu.permission in permissions]


async def bind_fresh_role_agents(db: AsyncSession, admin_role: Role) -> None:
    """fresh install 显式绑定全部当前启用的已发布 Agent。"""
    agents = (
        (
            await db.execute(
                select(AiAgent).where(
                    AiAgent.code.in_(PUBLISHED_AGENT_CODES),
                    AiAgent.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    db.add_all(
        [
            RoleAiAgent(
                role_id=admin_role.role_id,
                agent_id=agent.agent_id,
                enabled=True,
            )
            for agent in agents
        ]
    )


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

        # 创建初始 sys_config，包括用户导入默认密码。
        db.add_all(init_configs)

        # 创建与既有 R_* 编码契约一致的初始角色
        admin_role, default_user_role = build_init_roles()
        bind_fresh_role_permissions(admin_role, init_menus)
        db.add_all([admin_role, default_user_role])
        await db.flush()
        await seed_ai_agents_in_session(db)
        await bind_fresh_role_agents(db, admin_role)

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
