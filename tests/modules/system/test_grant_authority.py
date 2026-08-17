"""Phase 2 immutable GrantAuthority and dominance tests."""

from unittest.mock import AsyncMock, patch

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.core.id_generator import next_id
from app.db.base import user_depts
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.grant_authority import grant_authority_service


async def test_build_freezes_explicit_authority_and_materialized_scope(
    db_session: AsyncSession,
) -> None:
    marker = next_id()
    own_dept = Dept(
        dept_id=next_id(),
        dept_name=f"grant-own-{marker}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    visible_menu = Menu(
        menu_id=next_id(),
        menu_name=f"grant-visible-{marker}",
        menu_type="F",
        permission=f"qa:grant:{marker}:visible",
        status=STATUS_ENABLED,
    )
    disabled_menu = Menu(
        menu_id=next_id(),
        menu_name=f"grant-disabled-{marker}",
        menu_type="F",
        permission=f"qa:grant:{marker}:disabled",
        status=STATUS_DISABLED,
    )
    role = Role(
        role_id=next_id(),
        role_name=f"grant-role-{marker}",
        role_code=f"R_GRANT_{marker}",
        data_scope=DATA_SCOPE_DEPT,
        status=STATUS_ENABLED,
    )
    role.menus = [visible_menu, disabled_menu]
    actor = User(
        user_id=next_id(),
        user_name=f"grant-actor-{marker}",
        nickname="grant actor",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    actor.roles = [role]
    actor.depts = [own_dept]
    coworker = User(
        user_id=next_id(),
        user_name=f"grant-coworker-{marker}",
        nickname="grant coworker",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    agent = AiAgent(
        agent_id=next_id(),
        code=f"grant-agent-{marker}",
        name="Grant agent",
        description="Grant agent",
        enabled=True,
    )
    latent_agent = AiAgent(
        agent_id=next_id(),
        code=f"grant-latent-agent-{marker}",
        name="Latent grant agent",
        description="Latent grant agent",
        enabled=False,
    )
    db_session.add_all(
        [
            own_dept,
            visible_menu,
            disabled_menu,
            role,
            actor,
            coworker,
            agent,
            latent_agent,
        ]
    )
    await db_session.flush()
    await db_session.execute(
        insert(user_depts).values(
            user_id=coworker.user_id,
            dept_id=own_dept.dept_id,
            is_primary="N",
        )
    )
    active_binding = RoleAiAgent(
        role_id=role.role_id,
        agent_id=agent.agent_id,
        enabled=True,
    )
    db_session.add_all(
        [
            active_binding,
            RoleAiAgent(
                role_id=role.role_id,
                agent_id=latent_agent.agent_id,
                enabled=False,
            ),
        ]
    )
    await db_session.flush()

    with patch(
        "app.modules.ai.service.agent_authorization_service."
        "agent_authorization_service.list_agents",
        AsyncMock(return_value=[agent]),
    ):
        first = await grant_authority_service.build(db_session, actor.user_id)
        second = await grant_authority_service.build(db_session, actor.user_id)
        active_binding.enabled = False
        await db_session.flush()
        changed = await grant_authority_service.build(db_session, actor.user_id)

    assert first.actor_user_id == actor.user_id
    assert first.tenant_id == 0
    assert first.actor_status == STATUS_ENABLED
    assert first.enabled_role_ids == frozenset({role.role_id})
    assert first.permission_codes == frozenset(
        {visible_menu.permission, disabled_menu.permission}
    )
    assert first.menu_ids == frozenset({visible_menu.menu_id, disabled_menu.menu_id})
    assert first.visible_agent_ids == frozenset({agent.agent_id})
    assert first.grantable_agent_ids == frozenset({agent.agent_id})
    assert first.scope_kinds == frozenset({DATA_SCOPE_DEPT})
    assert first.accessible_dept_ids == frozenset({own_dept.dept_id})
    assert {actor.user_id, coworker.user_id} <= first.accessible_user_scope
    assert len(first.version_summary) == 64
    assert first.version_summary == second.version_summary
    assert changed.grantable_agent_ids == frozenset()
    assert changed.version_summary != first.version_summary


async def test_collection_and_scope_dominance_use_subset_semantics(
    db_session: AsyncSession,
) -> None:
    marker = next_id()
    own_dept = Dept(
        dept_id=next_id(),
        dept_name=f"dominance-own-{marker}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    role = Role(
        role_id=next_id(),
        role_name=f"dominance-role-{marker}",
        role_code=f"R_DOMINANCE_{marker}",
        data_scope=DATA_SCOPE_DEPT,
        status=STATUS_ENABLED,
    )
    permission = Menu(
        menu_id=next_id(),
        menu_name=f"dominance-menu-{marker}",
        menu_type="F",
        permission=f"qa:dominance:{marker}:use",
        status=STATUS_ENABLED,
    )
    role.menus = [permission]
    actor = User(
        user_id=next_id(),
        user_name=f"dominance-actor-{marker}",
        nickname="dominance actor",
        hashed_password="x",
        status=STATUS_ENABLED,
    )
    actor.roles = [role]
    actor.depts = [own_dept]
    db_session.add_all([own_dept, role, permission, actor])
    await db_session.flush()

    with patch(
        "app.modules.ai.service.agent_authorization_service."
        "agent_authorization_service.list_agents",
        AsyncMock(return_value=[]),
    ):
        authority = await grant_authority_service.build(db_session, actor.user_id)

    assert authority.allows_permission_codes({permission.permission}) is True
    assert authority.allows_permission_codes({"qa:outside:permission"}) is False
    assert authority.allows_menu_ids({permission.menu_id}) is True
    assert authority.allows_menu_ids({next_id()}) is False
    assert authority.allows_agent_ids(set()) is True
    assert authority.allows_scope_kind(DATA_SCOPE_CUSTOM, {own_dept.dept_id}) is True
    assert authority.allows_scope_kind(DATA_SCOPE_CUSTOM, {next_id()}) is False
    assert authority.allows_scope_kind(DATA_SCOPE_ALL, set()) is False
    assert (
        authority.allows_materialized_scope(
            dept_ids={own_dept.dept_id},
            user_ids={actor.user_id},
        )
        is True
    )
    assert (
        authority.allows_materialized_scope(
            dept_ids={next_id()},
            user_ids={actor.user_id},
        )
        is False
    )
