"""Task 14 Role-Agent delegation policy tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value
from tenant_helpers import bind_test_user, tenant_context

from app.constants import (
    ADMIN_USERNAME,
    DATA_SCOPE_ALL,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.id_generator import next_id
from app.db.session import AsyncSessionLocal, get_db
from app.main import app
from app.modules.ai.api.role_agent import put_role_agent_binding
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.schemas.role_agent import RoleAgentBindReq
from app.modules.ai.service.role_agent import role_agent_service
from app.modules.auth.service import get_current_tenant_context, get_current_user
from app.modules.system.models.menu import Menu
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import authorization_lock_service
from app.modules.system.service.role_delegation_service import (
    role_delegation_service,
)
from app.modules.system.service.role_management_service import role_management_service
from app.modules.system.service.tenant_association_writer import (
    replace_role_menus,
    replace_user_roles,
)

ROLE_AGENT_PERMISSION = "system:role:ai-agent-auth"


def _tenant(actor_user_id: int):
    return tenant_context(tenant_id=0, actor_user_id=actor_user_id)


async def test_role_agent_read_rechecks_current_role_delegation_policy(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, blocked = await _seed_role_agent_case(
        db_session
    )
    db_session.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=target_role.role_id,
            agent_id=delegated.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    with patch.object(
        role_management_service,
        "authorize_role_projection",
        AsyncMock(return_value=target_role),
    ) as authorize:
        binding = await role_agent_service.get_binding(
            db_session,
            target_role.role_id,
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    authorize.assert_awaited_once_with(
        db_session,
        actor_user_id=actor.user_id,
        role_id=target_role.role_id,
        tenant=_tenant(actor.user_id),
    )
    assert binding.bound_agent_ids == [str(delegated.agent_id)]
    assert {row.agent_id for row in binding.all_agents} == {delegated.agent_id}
    assert blocked.agent_id not in {row.agent_id for row in binding.all_agents}


def _menu(permission: str) -> Menu:
    marker = next_id()
    return Menu(
        tenant_id=0,
        menu_id=marker,
        menu_name=f"task14-menu-{marker}",
        menu_type="F",
        permission=permission,
        status=STATUS_ENABLED,
    )


def _role(
    code: str,
    *,
    data_scope: str = DATA_SCOPE_SELF,
    menus: list[Menu] | None = None,
) -> Role:
    marker = next_id()
    role = Role(
        tenant_id=0,
        role_id=marker,
        role_name=f"task14-role-{marker}",
        role_code=code,
        data_scope=data_scope,
        status=STATUS_ENABLED,
    )
    role.menus = menus or []
    return role


def _user(name: str, roles: list[Role]) -> User:
    return User(
        tenant_id=0,
        user_id=next_id(),
        user_name=name,
        nickname=name,
        hashed_password="x",
        status=STATUS_ENABLED,
        roles=roles,
    )


def _agent(code: str, *, enabled: bool = True) -> AiAgent:
    return AiAgent(
        agent_id=next_id(),
        code=code,
        name=code,
        description=f"Task 14 {code}",
        enabled=enabled,
        is_builtin=False,
        display_order=0,
        system_prompt="",
        risk_appetite="balanced",
    )


async def _active_agent_ids(db: AsyncSession, role_id: int) -> list[int]:
    return list(
        (
            await db.execute(
                select(RoleAiAgent.agent_id)
                .where(
                    RoleAiAgent.tenant_id == 0,
                    RoleAiAgent.role_id == role_id,
                    RoleAiAgent.enabled.is_(True),
                )
                .order_by(RoleAiAgent.agent_id)
            )
        ).scalars()
    )


async def _persist_role_graph(
    db: AsyncSession,
    *,
    users: list[User],
    roles: list[Role],
) -> None:
    user_role_links = {user.user_id: list(user.roles) for user in users}
    all_roles = {
        role.role_id: role
        for role in [*roles, *(role for user in users for role in user.roles)]
    }
    role_menu_links = {role.role_id: list(role.menus) for role in all_roles.values()}
    all_menus = {
        menu.menu_id: menu for role in all_roles.values() for menu in role.menus
    }
    for user in users:
        set_committed_value(user, "roles", [])
    for role in all_roles.values():
        set_committed_value(role, "menus", [])
    db.add_all([*all_menus.values(), *all_roles.values(), *users])
    await db.flush()
    tenant = _tenant(users[0].user_id)
    for role in all_roles.values():
        await replace_role_menus(
            db,
            role,
            role_menu_links[role.role_id],
            tenant=tenant,
        )
    for user in users:
        await replace_user_roles(
            db,
            user,
            user_role_links[user.user_id],
            tenant=tenant,
        )


async def _persist_user_roles(
    db: AsyncSession,
    user: User,
    roles: list[Role],
    *,
    actor_user_id: int,
) -> None:
    set_committed_value(user, "roles", [])
    db.add(user)
    await db.flush()
    await replace_user_roles(
        db,
        user,
        roles,
        tenant=_tenant(actor_user_id),
    )


async def _seed_role_agent_case(
    db: AsyncSession,
    *,
    actor_scope: str = DATA_SCOPE_ALL,
    actor_binding_enabled: bool = True,
    delegated_agent_enabled: bool = True,
) -> tuple[User, Role, Role, AiAgent, AiAgent]:
    permission = _menu(ROLE_AGENT_PERMISSION)
    actor_role = _role(
        f"R_TASK14_ACTOR_{next_id()}",
        data_scope=actor_scope,
        menus=[permission],
    )
    target_role = _role(f"R_TASK14_TARGET_{next_id()}")
    delegated = _agent(
        f"task14_delegated_{next_id()}",
        enabled=delegated_agent_enabled,
    )
    blocked = _agent(f"task14_blocked_{next_id()}")
    actor = _user(f"task14-actor-{next_id()}", [actor_role])
    await _persist_role_graph(
        db,
        users=[actor],
        roles=[actor_role, target_role],
    )
    bind_test_user(actor)
    db.add_all([delegated, blocked])
    await db.flush()
    db.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=actor_role.role_id,
            agent_id=delegated.agent_id,
            enabled=actor_binding_enabled,
        )
    )
    await db.flush()
    return actor, actor_role, target_role, delegated, blocked


async def test_ordinary_admin_can_replace_agents_within_delegation_ceiling(
    db_session: AsyncSession,
) -> None:
    (
        actor,
        actor_role,
        target_role,
        delegated,
        replacement,
    ) = await _seed_role_agent_case(db_session)
    db_session.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=actor_role.role_id,
            agent_id=replacement.agent_id,
            enabled=True,
        )
    )
    db_session.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=target_role.role_id,
            agent_id=delegated.agent_id,
            enabled=True,
        )
    )
    member = _user(f"task14-member-{next_id()}", [target_role])
    await _persist_user_roles(
        db_session,
        member,
        [target_role],
        actor_user_id=actor.user_id,
    )
    await db_session.flush()

    await role_agent_service.put_binding(
        db_session,
        target_role.role_id,
        RoleAgentBindReq(agent_ids=[str(replacement.agent_id)]),
        actor_user_id=actor.user_id,
        tenant=_tenant(actor.user_id),
    )

    assert await _active_agent_ids(db_session, target_role.role_id) == [
        replacement.agent_id
    ]


async def test_role_agent_replacement_rejects_new_agent_above_actor_ceiling(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, blocked = await _seed_role_agent_case(
        db_session
    )
    db_session.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=target_role.role_id,
            agent_id=delegated.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[str(blocked.agent_id)]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_AGENT_AUTHORITY_EXCEEDED"
    assert await _active_agent_ids(db_session, target_role.role_id) == [
        delegated.agent_id
    ]


async def test_role_agent_replacement_rejects_unowned_old_agent_removal(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, _delegated, blocked = await _seed_role_agent_case(
        db_session
    )
    db_session.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=target_role.role_id,
            agent_id=blocked.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_AGENT_AUTHORITY_EXCEEDED"
    assert await _active_agent_ids(db_session, target_role.role_id) == [
        blocked.agent_id
    ]


async def test_role_agent_replacement_rejects_member_outside_actor_scope(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session, actor_scope=DATA_SCOPE_SELF
    )
    member = _user(f"task14-member-{next_id()}", [target_role])
    await _persist_user_roles(
        db_session,
        member,
        [target_role],
        actor_user_id=actor.user_id,
    )
    db_session.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=target_role.role_id,
            agent_id=delegated.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE"
    assert await _active_agent_ids(db_session, target_role.role_id) == [
        delegated.agent_id
    ]


async def test_role_agent_replacement_rejects_self_mutation(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session
    )
    await replace_user_roles(
        db_session,
        actor,
        [*actor.roles, target_role],
        tenant=_tenant(actor.user_id),
    )
    db_session.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=target_role.role_id,
            agent_id=delegated.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_SELF_MUTATION_FORBIDDEN"


async def test_role_agent_replacement_rejects_protected_role(
    db_session: AsyncSession,
) -> None:
    (
        actor,
        _actor_role,
        _target_role,
        _delegated,
        _blocked,
    ) = await _seed_role_agent_case(db_session)
    protected_role = await db_session.scalar(
        select(Role).where(
            Role.tenant_id == 0,
            Role.role_code == SUPER_ADMIN_ROLE_CODE,
        )
    )
    assert protected_role is not None

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            protected_role.role_id,
            RoleAgentBindReq(agent_ids=[]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_PROTECTED"


async def test_role_agent_replacement_rejects_duplicate_complete_set(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(
                agent_ids=[str(delegated.agent_id), str(delegated.agent_id)]
            ),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_AGENT_SET_DUPLICATE"


async def test_globally_disabled_but_explicitly_bound_agent_can_be_removed(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session,
        delegated_agent_enabled=False,
    )
    db_session.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=target_role.role_id,
            agent_id=delegated.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    await role_agent_service.put_binding(
        db_session,
        target_role.role_id,
        RoleAgentBindReq(agent_ids=[]),
        actor_user_id=actor.user_id,
        tenant=_tenant(actor.user_id),
    )

    assert await _active_agent_ids(db_session, target_role.role_id) == []


async def test_soft_disabled_actor_binding_grants_no_delegation_authority(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session,
        actor_binding_enabled=False,
    )
    db_session.add(
        RoleAiAgent(
            tenant_id=0,
            role_id=target_role.role_id,
            agent_id=delegated.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_AGENT_AUTHORITY_EXCEEDED"


async def test_role_agent_service_rechecks_entry_permission(
    db_session: AsyncSession,
) -> None:
    actor, actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session
    )
    await replace_role_menus(
        db_session,
        actor_role,
        [],
        tenant=_tenant(actor.user_id),
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[str(delegated.agent_id)]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "MISSING_PERMISSION"


async def test_role_agent_service_authorizes_before_role_existence_lookup(
    db_session: AsyncSession,
) -> None:
    actor, actor_role, _target_role, _delegated, _blocked = await _seed_role_agent_case(
        db_session
    )
    await replace_role_menus(
        db_session,
        actor_role,
        [],
        tenant=_tenant(actor.user_id),
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            next_id(),
            RoleAgentBindReq(agent_ids=[]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "MISSING_PERMISSION"


async def test_role_agent_service_authorizes_before_agent_existence_lookup(
    db_session: AsyncSession,
) -> None:
    actor, actor_role, target_role, _delegated, _blocked = await _seed_role_agent_case(
        db_session
    )
    await replace_role_menus(
        db_session,
        actor_role,
        [],
        tenant=_tenant(actor.user_id),
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[str(next_id())]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "MISSING_PERMISSION"


async def test_target_role_definition_must_be_below_actor_ceiling(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session
    )
    blocked_menu = _menu(f"task14:blocked:{next_id()}:edit")
    db_session.add(blocked_menu)
    await db_session.flush()
    await replace_role_menus(
        db_session,
        target_role,
        [*target_role.menus, blocked_menu],
        tenant=_tenant(actor.user_id),
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[str(delegated.agent_id)]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_AGENT_AUTHORITY_EXCEEDED"


async def test_target_role_scope_template_must_be_below_actor_ceiling(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session, actor_scope=DATA_SCOPE_SELF
    )
    target_role.data_scope = DATA_SCOPE_ALL
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[str(delegated.agent_id)]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_AGENT_AUTHORITY_EXCEEDED"


async def test_member_complete_authority_is_checked_before_and_after(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, blocked = await _seed_role_agent_case(
        db_session
    )
    high_role = _role(f"R_TASK14_HIGH_{next_id()}")
    member = _user(f"task14-high-member-{next_id()}", [target_role, high_role])
    await _persist_role_graph(
        db_session,
        users=[member],
        roles=[target_role, high_role],
    )
    db_session.add_all(
        [
            RoleAiAgent(
                tenant_id=0,
                role_id=target_role.role_id,
                agent_id=delegated.agent_id,
                enabled=True,
            ),
            RoleAiAgent(
                tenant_id=0,
                role_id=high_role.role_id,
                agent_id=blocked.agent_id,
                enabled=True,
            ),
        ]
    )
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE"
    assert await _active_agent_ids(db_session, target_role.role_id) == [
        delegated.agent_id
    ]


async def test_admin_member_protects_role_from_ordinary_mutation(
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session
    )
    admin = await db_session.scalar(
        select(User)
        .where(
            User.tenant_id == 0,
            User.user_name == ADMIN_USERNAME,
        )
        .options(selectinload(User.roles))
        .execution_options(populate_existing=True)
    )
    assert admin is not None
    await replace_user_roles(
        db_session,
        admin,
        [*admin.roles, target_role],
        tenant=_tenant(actor.user_id),
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[str(delegated.agent_id)]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE"


async def test_super_admin_can_modify_protected_role(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await db_session.scalar(
        select(User)
        .where(
            User.tenant_id == 0,
            User.user_name == ADMIN_USERNAME,
        )
        .options(selectinload(User.roles))
        .execution_options(populate_existing=True)
    )
    protected_role = await db_session.scalar(
        select(Role).where(
            Role.tenant_id == 0,
            Role.role_code == SUPER_ADMIN_ROLE_CODE,
        )
    )
    assert admin is not None
    assert protected_role is not None

    async def _unexpected_member_scan(*_args, **_kwargs):
        raise AssertionError("super-admin replacement must not scan role members")

    monkeypatch.setattr(
        role_delegation_service,
        "_load_members",
        _unexpected_member_scan,
    )

    await role_agent_service.put_binding(
        db_session,
        protected_role.role_id,
        RoleAgentBindReq(agent_ids=[]),
        actor_user_id=admin.user_id,
        tenant=_tenant(admin.user_id),
    )

    assert await _active_agent_ids(db_session, protected_role.role_id) == []


async def test_member_phantom_after_preload_fails_closed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, _actor_role, target_role, delegated, _blocked = await _seed_role_agent_case(
        db_session
    )
    original_lock_targets = authorization_lock_service.lock_targets

    async def _inject_member_then_lock(*args, **kwargs):
        phantom = _user(f"task14-phantom-{next_id()}", [target_role])
        await _persist_user_roles(
            db_session,
            phantom,
            [target_role],
            actor_user_id=actor.user_id,
        )
        return await original_lock_targets(*args, **kwargs)

    monkeypatch.setattr(
        authorization_lock_service,
        "lock_targets",
        _inject_member_then_lock,
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        await role_agent_service.put_binding(
            db_session,
            target_role.role_id,
            RoleAgentBindReq(agent_ids=[str(delegated.agent_id)]),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor.user_id),
        )

    assert exc_info.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
    assert await _active_agent_ids(db_session, target_role.role_id) == []


async def test_role_agent_api_passes_authenticated_actor_to_policy() -> None:
    db = AsyncMock()
    current_user = SimpleNamespace(user_id=741)
    request = RoleAgentBindReq(agent_ids=[])

    with patch.object(
        role_agent_service,
        "put_binding",
        AsyncMock(return_value=None),
    ) as put_binding:
        response = await put_role_agent_binding(
            role_id=852,
            req=request,
            db=db,
            current_user=current_user,
            tenant=_tenant(741),
        )

    assert response.code == 200
    put_binding.assert_awaited_once_with(
        db,
        852,
        request,
        actor_user_id=741,
        tenant=_tenant(741),
    )
    db.commit.assert_awaited_once_with()


async def test_put_endpoint_applies_ordinary_admin_delegation_policy(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    actor, _actor_role, target_role, delegated, blocked = await _seed_role_agent_case(
        db_session
    )
    audit_path = f"/ai/role-agent/{target_role.role_id}"

    async def _override_db():
        yield db_session

    async def _override_current_user():
        return actor

    async def _override_tenant():
        return _tenant(actor.user_id)

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = _override_current_user
    app.dependency_overrides[get_current_tenant_context] = _override_tenant
    try:
        allowed = await client.put(
            audit_path,
            json={"agentIds": [str(delegated.agent_id)]},
        )
        assert allowed.status_code == 200

        denied = await client.put(
            audit_path,
            json={"agentIds": [str(blocked.agent_id)]},
        )
        assert denied.status_code == 403
        assert denied.json()["errorCode"] == "AI_ROLE_AGENT_AUTHORITY_EXCEEDED"
        assert await _active_agent_ids(db_session, target_role.role_id) == [
            delegated.agent_id
        ]
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_current_tenant_context, None)
        async with AsyncSessionLocal() as cleanup:
            await cleanup.execute(
                delete(SysOperationLog).where(
                    SysOperationLog.tenant_id == 0,
                    SysOperationLog.path == audit_path,
                )
            )
            await cleanup.commit()
