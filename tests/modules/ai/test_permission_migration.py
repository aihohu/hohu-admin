"""AI MVP 入口权限 upgrade 兼容矩阵。"""

from sqlalchemy import select

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.id_generator import next_id
from app.core.tenant import DEFAULT_TENANT_ID
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
from scripts.migrate_ai_mvp_permissions import migrate_ai_mvp_permissions


async def test_upgrade_grants_super_and_non_shared_bound_roles_only(db_session) -> None:
    marker = next_id()
    business_role = Role(
        tenant_id=DEFAULT_TENANT_ID,
        role_id=next_id(),
        role_name=f"AI business {marker}",
        role_code=f"R_AI_BUSINESS_{marker}",
        status=STATUS_ENABLED,
    )
    shared_only_role = Role(
        tenant_id=DEFAULT_TENANT_ID,
        role_id=next_id(),
        role_name=f"AI shared {marker}",
        role_code=f"R_AI_SHARED_{marker}",
        status=STATUS_ENABLED,
    )
    business_agent = AiAgent(
        agent_id=next_id(),
        code=f"business_{marker}",
        name="Business",
        description="Business agent",
        enabled=True,
    )
    shared_agent = await db_session.scalar(
        select(AiAgent).where(AiAgent.code == "shared")
    )
    if shared_agent is None:
        shared_agent = AiAgent(
            agent_id=next_id(),
            code="shared",
            name="Shared",
            description="Shared agent",
            enabled=True,
        )
        db_session.add(shared_agent)
    db_session.add_all([business_role, shared_only_role, business_agent])
    await db_session.flush()
    db_session.add_all(
        [
            RoleAiAgent(
                tenant_id=DEFAULT_TENANT_ID,
                role_id=business_role.role_id,
                agent_id=business_agent.agent_id,
                enabled=True,
            ),
            RoleAiAgent(
                tenant_id=DEFAULT_TENANT_ID,
                role_id=shared_only_role.role_id,
                agent_id=shared_agent.agent_id,
                enabled=True,
            ),
        ]
    )
    await db_session.flush()

    await migrate_ai_mvp_permissions(db_session)
    second = await migrate_ai_mvp_permissions(db_session)

    permission_menu = await db_session.scalar(
        select(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID,
            Menu.permission == AI_CHAT_USE_PERMISSION,
        )
    )
    super_role_id = await db_session.scalar(
        select(Role.role_id).where(
            Role.tenant_id == DEFAULT_TENANT_ID,
            Role.role_code == SUPER_ADMIN_ROLE_CODE,
        )
    )
    granted_role_ids = set(
        (
            await db_session.execute(
                select(role_menus.c.role_id).where(
                    role_menus.c.tenant_id == DEFAULT_TENANT_ID,
                    role_menus.c.menu_id == permission_menu.menu_id,
                )
            )
        ).scalars()
    )

    assert super_role_id in granted_role_ids
    assert business_role.role_id in granted_role_ids
    assert shared_only_role.role_id not in granted_role_ids
    assert second.roles_granted == 0

    # upgrade 为超管补 Agent 管理与 file.parse 权限，但不授给普通角色。
    super_permissions = set(
        (
            await db_session.execute(
                select(Menu.permission)
                .join(role_menus, role_menus.c.menu_id == Menu.menu_id)
                .where(
                    Menu.tenant_id == DEFAULT_TENANT_ID,
                    role_menus.c.tenant_id == DEFAULT_TENANT_ID,
                    role_menus.c.role_id == super_role_id,
                )
            )
        ).scalars()
    )
    assert {
        AI_CHAT_USE_PERMISSION,
        AI_FILE_PARSE_PERMISSION,
        AI_AGENT_EDIT_PERMISSION,
        "ai:agent:list",
        "system:dept:add",
        "system:dept:batch-delete",
        "system:dept:delete",
        "system:dept:edit",
        "system:dept:list",
        "system:dept:move",
        "system:role:add",
        "system:role:ai-agent-auth",
        "system:role:batch-delete",
        "system:role:delete",
        "system:role:edit",
        "system:role:list",
        "system:role:menu-auth",
        "system:user:add",
        "system:user:delete",
        "system:user:edit",
        "system:user:export",
        "system:user:import",
        "system:user:list",
        "system:user:reset-password",
        "system:user:role-auth",
    } <= super_permissions

    published_agent_ids = set(
        (
            await db_session.execute(
                select(AiAgent.agent_id).where(AiAgent.code.in_(PUBLISHED_AGENT_CODES))
            )
        ).scalars()
    )
    super_bound_ids = set(
        (
            await db_session.execute(
                select(RoleAiAgent.agent_id).where(
                    RoleAiAgent.tenant_id == DEFAULT_TENANT_ID,
                    RoleAiAgent.role_id == super_role_id,
                    RoleAiAgent.enabled.is_(True),
                )
            )
        ).scalars()
    )
    assert published_agent_ids <= super_bound_ids


async def test_upgrade_creates_disabled_enabled_tools_config_without_overwrite(
    db_session,
) -> None:
    existing = await db_session.scalar(
        select(Config).where(
            Config.tenant_id == DEFAULT_TENANT_ID,
            Config.config_key == "ai:enabled_tools",
        )
    )
    if existing is not None:
        existing.config_value = '["deployment.choice"]'
    await db_session.flush()

    await migrate_ai_mvp_permissions(db_session)

    config = await db_session.scalar(
        select(Config).where(
            Config.tenant_id == DEFAULT_TENANT_ID,
            Config.config_key == "ai:enabled_tools",
        )
    )
    assert config is not None
    expected = '["deployment.choice"]' if existing is not None else "[]"
    assert config.config_value == expected


async def test_upgrade_preserves_explicitly_disabled_super_agent_binding(
    db_session,
) -> None:
    super_role_id = await db_session.scalar(
        select(Role.role_id).where(
            Role.tenant_id == DEFAULT_TENANT_ID,
            Role.role_code == SUPER_ADMIN_ROLE_CODE,
        )
    )
    published_agent = await db_session.scalar(
        select(AiAgent).where(AiAgent.code.in_(PUBLISHED_AGENT_CODES))
    )
    binding = await db_session.scalar(
        select(RoleAiAgent).where(
            RoleAiAgent.role_id == super_role_id,
            RoleAiAgent.agent_id == published_agent.agent_id,
        )
    )
    if binding is None:
        binding = RoleAiAgent(
            tenant_id=DEFAULT_TENANT_ID,
            role_id=super_role_id,
            agent_id=published_agent.agent_id,
            enabled=False,
        )
        db_session.add(binding)
    else:
        binding.enabled = False
    await db_session.flush()

    await migrate_ai_mvp_permissions(db_session)
    await db_session.refresh(binding)

    assert binding.enabled is False


async def test_upgrade_normalizes_legacy_role_agent_authorization_label(
    db_session,
) -> None:
    menu = await db_session.scalar(
        select(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID,
            Menu.permission == "system:role:ai-agent-auth",
        )
    )
    assert menu is not None
    menu.menu_name = "Agent 授权"
    await db_session.flush()

    await migrate_ai_mvp_permissions(db_session)

    assert menu.menu_name == "AI Agent 授权"


async def test_upgrade_preserves_custom_role_agent_authorization_label(
    db_session,
) -> None:
    menu = await db_session.scalar(
        select(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID,
            Menu.permission == "system:role:ai-agent-auth",
        )
    )
    assert menu is not None
    menu.menu_name = "自定义 Agent 委派"
    await db_session.flush()

    await migrate_ai_mvp_permissions(db_session)

    assert menu.menu_name == "自定义 Agent 委派"
