"""Offline seeds require an issued stop fence and remain idempotent."""

import pytest
from sqlalchemy import delete, select

from app.core.exceptions import BusinessException
from app.db.authorization_seed_owner import seed_candidate_owner
from app.db.base import role_menus, user_roles
from app.modules.ai.models.agent import AiAgent
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from tests.modules.platform.test_authorization_principal_candidate import fence
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_seed_refuses_unproven_stopped_writers(system_sessions):
    factory, _ = system_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            await seed_candidate_owner.execute(db, action="seed.agents", fence=None)
        assert not db.in_transaction()


async def test_agent_seed_is_global_and_second_run_is_noop(system_sessions):
    factory, plain = system_sessions
    for _ in range(2):
        permit = await fence("seed.agents")
        async with factory() as db:
            await seed_candidate_owner.execute(db, action="seed.agents", fence=permit)
    async with plain() as db:
        assert len((await db.execute(select(AiAgent.agent_id))).all()) == 7
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2


async def test_settings_seed_preserves_existing_values(system_sessions):
    factory, plain = system_sessions
    for _ in range(2):
        permit = await fence("seed.settings")
        async with factory() as db:
            await seed_candidate_owner.execute(db, action="seed.settings", fence=permit)
    async with plain() as db:
        assert (
            await db.scalar(
                select(SystemSetting.setting_value).where(
                    SystemSetting.tenant_id == 0,
                    SystemSetting.setting_key == "site_name",
                )
            )
            is not None
        )
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2


async def test_menu_seed_does_not_create_other_seed_resources(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        before = [
            set(await db.scalars(select(model.__mapper__.primary_key[0])))
            for model in (User, AiAgent, SystemSetting)
        ]
    for _ in range(2):
        permit = await fence("seed.menus")
        async with factory() as db:
            result = await seed_candidate_owner.execute(
                db, action="seed.menus", fence=permit
            )
        assert result["fresh"] is False
    assert result["planned_changes"] == 0
    async with plain() as db:
        after = [
            set(await db.scalars(select(model.__mapper__.primary_key[0])))
            for model in (User, AiAgent, SystemSetting)
        ]
        assert after == before
        assert (
            await db.scalar(
                select(Menu.menu_id).where(
                    Menu.tenant_id == 0, Menu.route_name == "ai_chat"
                )
            )
            is not None
        )


@pytest.mark.parametrize("fresh", [False, True])
async def test_system_seed_converges_and_initializes_admin_only_when_fresh(
    system_sessions, fresh
):
    factory, plain = system_sessions
    if fresh:
        async with plain() as db:
            await db.execute(delete(user_roles).where(user_roles.c.tenant_id == 0))
            await db.execute(delete(role_menus).where(role_menus.c.tenant_id == 0))
            await db.execute(delete(User).where(User.tenant_id == 0))
            await db.execute(delete(Role).where(Role.tenant_id == 0))
            await db.execute(delete(Menu).where(Menu.tenant_id == 0))
            await db.commit()
    snapshots = []
    for attempt in range(2):
        permit = await fence("seed.system")
        async with factory() as db:
            result = await seed_candidate_owner.execute(
                db, action="seed.system", fence=permit, admin_password="SeedAdmin123!"
            )
            assert result["fresh"] is (fresh and attempt == 0)
            if attempt:
                assert result["planned_changes"] == 0
        async with plain() as db:
            snapshots.append(
                (
                    await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)),
                    (
                        await db.execute(
                            select(
                                UserFieldPolicyState.tenant_id,
                                UserFieldPolicyState.epoch,
                            ).order_by(UserFieldPolicyState.tenant_id)
                        )
                    ).all(),
                )
            )
            assert (
                await db.scalar(
                    select(Menu.menu_id).where(
                        Menu.tenant_id == 0, Menu.route_name == "ai_chat"
                    )
                )
                is not None
            )
            if fresh:
                assert (
                    await db.scalar(select(User.user_name).where(User.tenant_id == 0))
                    == "admin"
                )
    assert snapshots[0] == snapshots[1]
