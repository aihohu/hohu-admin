# ruff: noqa: T201

import asyncio

from sqlalchemy import insert, select, text
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
from app.core.security import get_password_hash
from app.core.tenant import DEFAULT_TENANT_CODE, DEFAULT_TENANT_ID
from app.db.base import role_menus, user_roles
from app.modules.ai.constants import (
    AI_CHAT_USE_PERMISSION,
    AI_FILE_PARSE_PERMISSION,
    PUBLISHED_AGENT_CODES,
    PUBLISHED_AGENT_TOOL_PERMISSIONS,
)
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.constants import (
    PHASE3_DESTRUCTIVE_PERMISSIONS,
)
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.utils.validators import validate_password
from scripts.seed_ai_agents import seed_ai_agents_in_session
from scripts.seed_config import seed_config_in_session
from scripts.sync_menus import sync_menus_in_session


def build_default_tenant() -> Tenant:
    """Build the single-mode compatibility tenant with its reserved ID."""
    return Tenant(
        tenant_id=DEFAULT_TENANT_ID,
        tenant_code=DEFAULT_TENANT_CODE,
        tenant_name="Default Tenant",
        status=STATUS_ENABLED,
        lifecycle_state="active",
        bootstrap_version=1,
        row_version=1,
    )


async def ensure_default_tenant(db: AsyncSession) -> Tenant:
    """Idempotently provide Default Tenant after migration or seed cleanup."""
    result = await db.execute(
        select(Tenant).where(Tenant.tenant_id == DEFAULT_TENANT_ID)
    )
    tenant = result.scalars().first()
    if tenant is None:
        tenant = build_default_tenant()
        db.add(tenant)
        await db.flush()
    return tenant


def build_init_roles() -> list[Role]:
    """构造与既有角色编码契约一致的 fresh-install 角色种子。"""
    return [
        Role(
            tenant_id=DEFAULT_TENANT_ID,
            role_name="系统超级管理员",
            role_code=SUPER_ADMIN_ROLE_CODE,
            status=STATUS_ENABLED,
        ),
        Role(
            tenant_id=DEFAULT_TENANT_ID,
            role_name="普通用户",
            role_code=USER_ROLE_CODE,
            role_desc="AI user.create 与普通账号使用的后端默认角色",
            data_scope=DATA_SCOPE_SELF,
            status=STATUS_ENABLED,
        ),
    ]


def fresh_role_permission_menus(menus: list[Menu]) -> list[Menu]:
    """Return every published permission menu for fresh R_SUPER."""
    permissions = {
        AI_CHAT_USE_PERMISSION,
        AI_FILE_PARSE_PERMISSION,
        *PUBLISHED_AGENT_TOOL_PERMISSIONS,
        *PHASE3_DESTRUCTIVE_PERMISSIONS,
    }
    return [menu for menu in menus if menu.permission in permissions]


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
                tenant_id=DEFAULT_TENANT_ID,
                role_id=admin_role.role_id,
                agent_id=agent.agent_id,
                enabled=True,
            )
            for agent in agents
        ]
    )


async def seed_database(db: AsyncSession, *, admin_password: str | None = None) -> bool:
    """Converge deployment data; the caller owns the transaction and rollback."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": 0x484F485553454544}
    )
    existing_user = await db.scalar(
        select(User.user_id).where(User.tenant_id == DEFAULT_TENANT_ID).limit(1)
    )
    fresh = existing_user is None
    if fresh:
        if not admin_password:
            raise ValueError("HOHU_ADMIN_PASSWORD is required for first installation")
        validate_password(admin_password)

    await ensure_default_tenant(db)
    menus = await sync_menus_in_session(db, tenant_id=DEFAULT_TENANT_ID)
    await seed_config_in_session(db, tenant_id=DEFAULT_TENANT_ID, fresh=fresh)
    await seed_ai_agents_in_session(db)
    if fresh:
        admin_role, default_role = build_init_roles()
        db.add_all([admin_role, default_role])
        await db.flush()
        await db.execute(
            insert(role_menus),
            [
                {
                    "tenant_id": DEFAULT_TENANT_ID,
                    "role_id": admin_role.role_id,
                    "menu_id": m.menu_id,
                }
                for m in fresh_role_permission_menus(menus)
            ],
        )
        await bind_fresh_role_agents(db, admin_role)
        user = User(
            tenant_id=DEFAULT_TENANT_ID,
            user_name="admin",
            nickname=ADMIN_ROLE_CODE,
            hashed_password=get_password_hash(admin_password),
            status=STATUS_ENABLED,
        )
        db.add(user)
        await db.flush()
        await db.execute(
            insert(user_roles).values(
                tenant_id=DEFAULT_TENANT_ID,
                user_id=user.user_id,
                role_id=admin_role.role_id,
            )
        )

    # Do not implicitly bootstrap prepared tenants or copy Default Tenant edits.
    tenant_ids = (
        await db.scalars(
            select(Tenant.tenant_id).where(
                Tenant.tenant_id != DEFAULT_TENANT_ID, Tenant.bootstrap_version > 0
            )
        )
    ).all()
    for tenant_id in tenant_ids:
        await sync_menus_in_session(db, tenant_id=tenant_id, hosted=True)
    await db.flush()
    return fresh


async def init_db():
    engine = create_async_engine(settings.DATABASE_URL)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db, db.begin():
            secret = settings.HOHU_ADMIN_PASSWORD
            fresh = await seed_database(
                db, admin_password=secret.get_secret_value() if secret else None
            )
        print("Initial data created." if fresh else "Deployment data synchronized.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(init_db())
