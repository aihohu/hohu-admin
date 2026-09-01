"""P1-B Agent 授权单一 Policy 回归测试。"""

import pytest
from sqlalchemy import delete, select
from tenant_helpers import bind_test_user

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.exceptions import AuthorizationException
from app.core.id_generator import next_id
from app.modules.ai.agents.tools import load_builtin_tools
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.tenant_association_writer import (
    replace_role_menus,
    replace_user_roles,
)


async def _principal(
    db,
    *,
    role_code: str,
    permissions: tuple[str, ...] = ("system:user:list",),
    bind_agent: bool = True,
    binding_enabled: bool = True,
    role_status: str = STATUS_ENABLED,
) -> tuple[User, Role, AiAgent]:
    load_builtin_tools()
    marker = str(next_id())
    agent = await db.scalar(select(AiAgent).where(AiAgent.code == "user_mgmt"))
    if agent is None:
        agent = AiAgent(
            code="user_mgmt",
            name="User",
            description="User management",
            enabled=True,
            is_builtin=True,
        )
        db.add(agent)
    else:
        agent.enabled = True

    menus: list[Menu] = []
    for permission in permissions:
        menu = await db.scalar(select(Menu).where(Menu.permission == permission))
        if menu is None:
            menu = Menu(
                tenant_id=0,
                menu_name=f"P1B {permission}",
                menu_type="F",
                permission=permission,
                status=STATUS_ENABLED,
            )
            db.add(menu)
        menus.append(menu)

    role = Role(
        tenant_id=0,
        role_name=f"P1B role {marker}",
        role_code=f"{role_code}_{marker}"
        if role_code != SUPER_ADMIN_ROLE_CODE
        else role_code,
        status=role_status,
    )
    if role_code == SUPER_ADMIN_ROLE_CODE:
        existing = await db.scalar(
            select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
        )
        if existing is not None:
            role = existing
            role.status = role_status
    user = User(
        tenant_id=0,
        user_name=f"p1b_{marker}",
        nickname="P1B",
        hashed_password="test",
        status=STATUS_ENABLED,
    )
    db.add_all([role, user])
    await db.flush()
    tenant = bind_test_user(user)
    await replace_role_menus(db, role, menus, tenant=tenant)
    await replace_user_roles(db, user, [role], tenant=tenant)
    if bind_agent:
        db.add(
            RoleAiAgent(
                tenant_id=tenant.tenant_id,
                role_id=role.role_id,
                agent_id=agent.agent_id,
                enabled=binding_enabled,
            )
        )
        await db.flush()
    else:
        await db.execute(
            delete(RoleAiAgent).where(
                RoleAiAgent.role_id == role.role_id,
                RoleAiAgent.agent_id == agent.agent_id,
            )
        )
    return user, role, agent


async def test_explicit_binding_and_visible_tool_are_both_required(db_session) -> None:
    user, _role, agent = await _principal(db_session, role_code="P1B_USER")

    authorized = await agent_authorization_service.authorize_agent_access(
        db_session,
        user,
        agent.code,
    )

    assert authorized.agent_id == agent.agent_id
    assert [
        item.code
        for item in await agent_authorization_service.list_agents(db_session, user)
    ] == ["user_mgmt"]


@pytest.mark.parametrize(
    ("bind_agent", "binding_enabled", "role_status", "permissions"),
    [
        (False, True, STATUS_ENABLED, ("system:user:list",)),
        (True, False, STATUS_ENABLED, ("system:user:list",)),
        (True, True, "2", ("system:user:list",)),
        (True, True, STATUS_ENABLED, ()),
    ],
)
async def test_missing_policy_layer_is_forbidden(
    db_session,
    bind_agent: bool,
    binding_enabled: bool,
    role_status: str,
    permissions: tuple[str, ...],
) -> None:
    user, _role, agent = await _principal(
        db_session,
        role_code="P1B_USER",
        bind_agent=bind_agent,
        binding_enabled=binding_enabled,
        role_status=role_status,
        permissions=permissions,
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await agent_authorization_service.authorize_agent_access(
            db_session,
            user,
            agent.code,
        )

    assert exc_info.value.error_code == "AI_AGENT_FORBIDDEN"


async def test_super_role_has_no_agent_binding_bypass(db_session) -> None:
    user, _role, agent = await _principal(
        db_session,
        role_code=SUPER_ADMIN_ROLE_CODE,
        bind_agent=False,
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await agent_authorization_service.authorize_agent_access(
            db_session,
            user,
            agent.code,
        )

    assert exc_info.value.error_code == "AI_AGENT_FORBIDDEN"


async def test_super_role_tool_permissions_use_explicit_collector(db_session) -> None:
    user, _role, _agent = await _principal(
        db_session,
        role_code=SUPER_ADMIN_ROLE_CODE,
        permissions=("ai:file:parse",),
        bind_agent=False,
    )

    assert agent_authorization_service.tool_permissions(user) == {"ai:file:parse"}


async def test_shared_agent_has_no_binding_bypass(db_session) -> None:
    user, _role, _agent = await _principal(
        db_session,
        role_code="P1B_SHARED",
        permissions=("ai:file:parse",),
        bind_agent=False,
    )
    shared = await db_session.scalar(select(AiAgent).where(AiAgent.code == "shared"))
    assert shared is not None
    shared.enabled = True

    with pytest.raises(AuthorizationException) as exc_info:
        await agent_authorization_service.authorize_agent_access(
            db_session,
            user,
            "shared",
        )

    assert exc_info.value.error_code == "AI_AGENT_FORBIDDEN"


async def test_grantable_agents_include_disabled_agent_but_not_disabled_binding(
    db_session,
) -> None:
    user, role, agent = await _principal(
        db_session,
        role_code="P2_GRANTABLE",
        binding_enabled=False,
    )
    agent.enabled = False
    outside = AiAgent(
        code=f"outside_{next_id()}",
        name="Outside",
        description="Outside",
        enabled=False,
    )
    db_session.add(outside)
    await db_session.flush()

    grantable = await agent_authorization_service.grantable_agent_ids(
        db_session,
        user,
    )

    assert grantable == set()
    assert role.status == STATUS_ENABLED
    assert outside.agent_id not in grantable

    binding = await db_session.scalar(
        select(RoleAiAgent).where(
            RoleAiAgent.role_id == role.role_id,
            RoleAiAgent.agent_id == agent.agent_id,
        )
    )
    assert binding is not None
    binding.enabled = True
    await db_session.flush()

    assert await agent_authorization_service.grantable_agent_ids(
        db_session,
        user,
    ) == {agent.agent_id}
