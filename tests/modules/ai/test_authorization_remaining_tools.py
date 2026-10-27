"""Remaining planned database tools preserve current policy and exact claims."""

import json

import pytest
from sqlalchemy import BigInteger, Column, MetaData, Table, insert, select, update

from app.core.authorization_plan import LockMode, TenantLock
from app.core.security import verify_password
from app.db.base import role_menus
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.service.authorization_execution_candidate import (
    ai_execution_candidate,
)
from app.modules.job import task_registry
from app.modules.job.models.job import SysJob
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.user import User
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.role_management_service import role_management_service
from app.modules.system.service.user_service import user_service
from tests.modules.ai.test_authorization_gateway_execution import (
    execute,
    rebind,
)
from tests.modules.ai.test_authorization_gateway_execution import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_recovery_candidate import (
    recovery_case as recovery_case,
)
from tests.modules.system.test_authorization_system_candidate import (
    epoch,
)
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)

PASSWORD = "Continuation876!"


async def test_batch_delete_composition(remaining_case):

    factory, plain, case = remaining_case
    async with plain() as db:
        snapshot = await user_service.get_batch_delete_identity_snapshot(
            db, [501], tenant=case.tenant
        )
        await rebind(
            db, case, "user.batch_delete", "user_mgmt", {"user_ids": [501]}, snapshot
        )
    async with factory() as db:
        command = await system_ai_tool_adapter.prepare_input(
            db,
            tool="user.batch_delete",
            args={"user_ids": [501]},
            snapshot=snapshot,
            tenant=case.tenant,
            actor_user_version=1,
        )
        async with authorization_transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            assert (
                await ai_execution_candidate.execute(
                    db,
                    tx,
                    command,
                    case.claim,
                    tenant=case.tenant,
                    actor_user_version=1,
                )
            ).disposition == "succeeded"
        await db.rollback()


@pytest.mark.parametrize(
    "stale,context_present", [(False, False), (False, True), (True, True)]
)
async def test_batch_delete_exact_identity_and_context(
    remaining_case, stale, context_present
):

    factory, plain, case = remaining_case
    async with plain() as db:
        if context_present:
            await db.execute(
                insert(AiConversation).values(
                    tenant_id=0, conversation_id=901, user_id=501, title="Target"
                )
            )
            await db.execute(
                insert(AiMessage).values(
                    tenant_id=0,
                    conversation_id=901,
                    message_id=902,
                    role="user",
                    content="Earlier",
                    is_active=True,
                )
            )
        snapshot = await user_service.get_batch_delete_identity_snapshot(
            db, [501], tenant=case.tenant
        )
        if stale:
            snapshot["targets"][0]["userName"] = "changed"
        await rebind(
            db, case, "user.batch_delete", "user_mgmt", {"user_ids": [501]}, snapshot
        )
    assert (await execute(factory, case)).disposition == (
        "failed" if stale else "succeeded"
    )
    async with plain() as db:
        assert (await db.get(User, 501) is not None) == stale
        if context_present:
            assert (await db.get(AiConversation, 901) is not None) == stale
            assert (await db.get(AiMessage, 902) is not None) == stale
        assert await epoch(db) == (1 if stale else 2)


async def test_user_preparation_leaves_no_open_transaction(remaining_case):
    factory, _, case = remaining_case
    async with factory() as db:
        command = await system_ai_tool_adapter.prepare_input(
            db,
            tool="user.reset_password",
            args={"user_id": 501},
            snapshot=None,
            tenant=case.tenant,
            actor_user_version=1,
        )
        assert command.command.values[6] and not db.in_transaction()


async def test_user_execution_composes_in_original_transaction(remaining_case):

    factory, plain, case = remaining_case
    async with plain() as db:
        await rebind(
            db, case, "user.reset_password", "user_mgmt", {"user_id": 501}, None
        )
    async with factory() as db:
        command = await system_ai_tool_adapter.prepare_input(
            db,
            tool="user.reset_password",
            args={"user_id": 501},
            snapshot=None,
            tenant=case.tenant,
            actor_user_version=1,
        )
        async with authorization_transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            outcome = await ai_execution_candidate.execute(
                db, tx, command, case.claim, tenant=case.tenant, actor_user_version=1
            )
        assert outcome.disposition == "succeeded"
        await db.rollback()


@pytest.fixture
async def remaining_case(execution_case):
    factory, plain, case = execution_case
    historical = MetaData()
    Table("mk_app", historical, Column("author_id", BigInteger))
    Table("mk_app_rating", historical, Column("user_id", BigInteger))
    async with plain.kw["bind"].begin() as connection:
        await connection.run_sync(historical.create_all)
    async with plain() as db:
        await db.execute(update(AiAgent).values(code="user_mgmt"))
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="Member",
                role_code="R_USER",
                data_scope="5",
                status="1",
            )
        )
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="target",
                hashed_password="old",
                status="1",
                auth_version=1,
            )
        )
        await db.execute(
            insert(SystemSetting).values(
                tenant_id=0,
                setting_key="auth:default_password",
                setting_value=PASSWORD,
                status="1",
            )
        )
        await db.commit()
    return factory, plain, case


@pytest.mark.parametrize(
    "tool,args",
    [
        ("user.create", {"user_name": "plannednew", "role_assignment": "DEFAULT"}),
        ("user.reset_password", {"user_id": 501}),
        ("user.update", {"user_id": 501, "nickname": "Updated"}),
    ],
)
async def test_remaining_user_tools_atomic_success(remaining_case, tool, args):
    factory, plain, case = remaining_case
    async with plain() as db:
        await rebind(db, case, tool, "user_mgmt", args, None)
    assert (await execute(factory, case)).disposition == "succeeded"
    async with plain() as db:
        user = await db.scalar(
            select(User).where(
                User.user_name == ("plannednew" if tool == "user.create" else "target")
            )
        )
        if tool in {"user.create", "user.reset_password"}:
            assert verify_password(PASSWORD, user.hashed_password)
        if tool == "user.reset_password":
            assert user.auth_version == 2
        if tool == "user.update":
            assert user.nickname == "Updated"
        action = await db.scalar(select(AiPreparedAction))
        assert action.status == "succeeded" and action.subject_refs_hash
        assert PASSWORD not in json.dumps(action.result_data)
        assert PASSWORD not in json.dumps(action.result_ui)
        assert await epoch(db) == 2
    assert (await execute(factory, case)).disposition == "succeeded"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("user.create", {"user_name": "plannednew"}),
        ("user.reset_password", {"user_id": 501}),
    ],
)
async def test_password_policy_rechecked_after_lock_free_preparation(
    remaining_case, monkeypatch, tool, args
):
    factory, plain, case = remaining_case
    async with plain() as db:
        await rebind(db, case, tool, "user_mgmt", args, None)
    original = system_ai_tool_adapter.plan

    async def changed(*a, **kw):
        async with plain() as db:
            await db.execute(
                update(SystemSetting)
                .where(SystemSetting.setting_key == "auth:default_password")
                .values(setting_value="Changed876!")
            )
            await db.commit()
        return await original(*a, **kw)

    monkeypatch.setattr(system_ai_tool_adapter, "plan", changed)
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert (
            await db.scalar(select(User.hashed_password).where(User.user_id == 501))
            == "old"
        )
        assert (
            await db.scalar(select(User.user_id).where(User.user_name == "plannednew"))
            is None
        )
        assert await epoch(db) == 1


async def test_ai_cannot_reset_own_password(remaining_case):
    factory, plain, case = remaining_case
    async with plain() as db:
        await rebind(
            db, case, "user.reset_password", "user_mgmt", {"user_id": 101}, None
        )
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert (
            await db.scalar(select(User.auth_version).where(User.user_id == 101)) == 1
        )


@pytest.mark.parametrize("stale", [False, True])
async def test_role_agent_replacement_composes_with_gateway(remaining_case, stale):
    factory, plain, case = remaining_case
    async with plain() as db:
        await db.execute(
            update(AiAgent).where(AiAgent.agent_id == 8001).values(code="role_mgmt")
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9002,
                menu_name="Agent grant",
                menu_type="F",
                permission="system:role:ai-agent-auth",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9002)
        )
        await db.execute(
            insert(AiAgent).values(
                agent_id=8002,
                code="dept_mgmt",
                name="Dept",
                description="Test",
                enabled=True,
            )
        )
        preview = await role_management_service.preview_update_agents(
            db, 301, [8002], actor_user_id=101, tenant=case.tenant
        )
        await rebind(
            db,
            case,
            "role.update_agents",
            "role_mgmt",
            {"role_id": 301, "agent_ids": [8002]},
            preview.snapshot,
        )
        if stale:
            await db.execute(update(Role).where(Role.role_id == 301).values(status="2"))
            await db.commit()
    assert (await execute(factory, case)).disposition == (
        "failed" if stale else "succeeded"
    )
    async with plain() as db:
        bindings = list(
            await db.scalars(
                select(RoleAiAgent.agent_id).where(RoleAiAgent.role_id == 301)
            )
        )
        assert bindings == ([] if stale else [8002])
        assert await epoch(db) == (1 if stale else 2)


@pytest.mark.parametrize(
    "cron,extra,success",
    [
        ("*/5 * * * *", {}, True),
        ("invalid", {}, False),
        ("*/5 * * * *", {"job_key": "injected"}, False),
    ],
)
async def test_job_cron_uses_fixed_resource_and_atomic_facts(
    remaining_case, monkeypatch, cron, extra, success
):
    factory, plain, case = remaining_case
    monkeypatch.setitem(
        task_registry._task_functions, "planned_cron_test", lambda: None
    )
    async with plain() as db:
        await db.execute(
            update(AiAgent).where(AiAgent.agent_id == 8001).values(code="job_mgmt")
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9002,
                menu_name="Job edit",
                menu_type="F",
                permission="system:job:edit",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9002)
        )
        await db.execute(
            insert(SysJob).values(
                tenant_id=0,
                job_id=701,
                job_key="planned_cron_test",
                job_name="Planned job",
                trigger_type="cron",
                cron_expression="0 * * * *",
                status="1",
            )
        )
        await rebind(
            db,
            case,
            "job.update_cron",
            "job_mgmt",
            {"job_id": 701, "cron_expression": cron} | extra,
            None,
        )
    assert (await execute(factory, case)).disposition == (
        "succeeded" if success else "failed"
    )
    async with plain() as db:
        assert await db.scalar(select(SysJob.cron_expression)) == (
            cron if success else "0 * * * *"
        )
        assert await epoch(db) == 1
