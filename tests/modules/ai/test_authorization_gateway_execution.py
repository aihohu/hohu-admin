"""Real planned Gateway writes, including atomic completion and no replay."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, insert, select, update

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.db.base import role_menus, user_roles
from app.modules.ai.agents.gateway import authorization_execution
from app.modules.ai.agents.gateway.authorization_execution import PlannedGatewayOwner
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.safety import ai_config
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.service.atomic_completion_service import ClaimBinding
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptCreate, DeptUpdate
from app.modules.system.schemas.role import RoleCreate, RoleUpdate
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from app.modules.system.service.authorization_transaction_service import (
    _Transaction,
    authorization_transactions,
)
from app.modules.system.service.dept_service import dept_service
from app.modules.system.service.grant_authority import grant_authority_service
from app.modules.system.service.role_management_service import role_management_service
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service,
)
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service,
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


@pytest.fixture
async def execution_case(recovery_case):
    factory, plain, case = recovery_case
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9001,
                menu_name="AI",
                menu_type="F",
                permission="ai:chat:use",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9001)
        )
        await db.execute(
            insert(AiAgent).values(
                agent_id=8001,
                code="dept_mgmt",
                name="Departments",
                description="Test",
                enabled=True,
            )
        )
        await db.execute(
            insert(RoleAiAgent).values(
                tenant_id=0,
                role_id=201,
                agent_id=8001,
                enabled=True,
            )
        )
        args = {"parent_id": None, "dept_name": "Gateway team", "status": "1"}
        preview, *_ = await dept_service._build_preview(
            db,
            tenant=case.tenant,
            action="create",
            actor_user_id=101,
            dept_in=DeptCreate(**args),
        )
        snapshot = {"business": preview.snapshot, "argsHash": compute_args_hash(args)}
        await db.execute(
            update(AiPreparedAction).values(
                execute_tool_name="dept.create",
                agent_code="dept_mgmt",
                frozen_args=args,
                args_hash=compute_args_hash(args),
                snapshot=snapshot,
                snapshot_hash=compute_args_hash(snapshot),
                tool_codes=["dept.create"],
            )
        )
        await db.execute(
            update(AiOperationLog).values(
                tool_name="dept.create",
                agent_code="dept_mgmt",
                args_hash=compute_args_hash(args),
            )
        )
        await db.commit()
        action = await db.scalar(
            select(AiPreparedAction).execution_options(populate_existing=True)
        )
        case.claim = ClaimBinding.from_action(action, case.log.log_id)
    return factory, plain, case


async def execute(factory, case):
    return await PlannedGatewayOwner(factory).execute_claim(
        case.claim,
        tenant=case.tenant,
        actor_user_version=1,
    )


async def test_cancelled_execution_survives_unknown_rollback(
    execution_case, monkeypatch
):
    factory, _, case = execution_case
    cancelled = asyncio.CancelledError("external cancellation")
    monkeypatch.setattr(
        authorization_execution.ai_execution_candidate,
        "reference",
        AsyncMock(side_effect=cancelled),
    )
    monkeypatch.setattr(
        authorization_execution,
        "rollback_authorization_session",
        AsyncMock(side_effect=RuntimeError("rollback acknowledgement unavailable")),
    )
    facts = AsyncMock()
    commit = AsyncMock()
    monkeypatch.setattr(PlannedGatewayOwner, "_facts", facts)
    monkeypatch.setattr(authorization_execution, "commit_authorization_session", commit)
    with pytest.raises(asyncio.CancelledError) as error:
        await execute(factory, case)
    assert error.value is cancelled
    facts.assert_not_called()
    commit.assert_not_called()


async def test_business_epoch_action_and_required_log_commit_together(execution_case):
    factory, plain, case = execution_case
    outcome = await execute(factory, case)
    assert outcome.disposition == "succeeded"
    assert not hasattr(outcome, "action")  # facts are not a current result projection
    async with plain() as db:
        assert await db.scalar(select(Dept.dept_name)) == "Gateway team"
        assert await epoch(db) == 2
        action = await db.scalar(select(AiPreparedAction))
        log = await db.scalar(select(AiOperationLog))
        assert action.status == "succeeded" and log.status == "success"
        assert action.result_data["deptName"] == "Gateway team"
        assert (
            action.execution_owner is None and action.execution_lease_expires_at is None
        )
        assert action.row_version == case.claim.row_version + 1


async def test_repeated_confirmation_does_not_apply_again(execution_case):
    factory, plain, case = execution_case
    assert (await execute(factory, case)).disposition == "succeeded"
    assert (await execute(factory, case)).disposition == "succeeded"
    async with plain() as db:
        assert len((await db.scalars(select(Dept))).all()) == 1
        assert await epoch(db) == 2


@pytest.mark.parametrize("revocation", ["user", "version", "agent", "grant", "chat"])
async def test_live_revocation_prevents_business_write(execution_case, revocation):
    factory, plain, case = execution_case
    async with plain() as db:
        if revocation == "user":
            await db.execute(update(User).values(status="2"))
        elif revocation == "version":
            await db.execute(update(User).values(auth_version=2))
        elif revocation == "agent":
            await db.execute(update(AiAgent).values(enabled=False))
        elif revocation == "grant":
            await db.execute(update(RoleAiAgent).values(enabled=False))
        else:
            await db.execute(delete(role_menus).where(role_menus.c.menu_id == 9001))
        await db.commit()
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert await db.scalar(select(Dept)) is None
        assert await epoch(db) == 1


async def test_required_log_failure_rolls_back_business_and_epoch(
    execution_case, monkeypatch
):

    factory, plain, case = execution_case
    original = _Transaction.update_factual

    async def fail_success_log(self, key, values, **kwargs):
        if key.kind.value == "operation_log" and values.get("status") == "success":
            raise RuntimeError("required log unavailable")
        return await original(self, key, values, **kwargs)

    monkeypatch.setattr(_Transaction, "update_factual", fail_success_log)
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert await db.scalar(select(Dept)) is None
        assert await epoch(db) == 1
        assert await db.scalar(select(AiPreparedAction.status)) == "failed"


async def test_lost_commit_ack_never_replays(execution_case, monkeypatch):
    factory, plain, case = execution_case

    def uncertain_factory():
        db = factory()
        commit = db.commit

        async def lose_ack():
            await commit()
            raise ConnectionError("ACK lost")

        monkeypatch.setattr(db, "commit", lose_ack)
        return db

    assert (await execute(uncertain_factory, case)).disposition == "unknown"
    assert (await execute(factory, case)).disposition == "succeeded"
    async with plain() as db:
        assert len((await db.scalars(select(Dept))).all()) == 1
        assert await epoch(db) == 2


async def test_guard_barrier_and_context_precede_domain_locks(execution_case):
    factory, plain, case = execution_case
    sql = []

    def record(_c, _cursor, statement, _p, _ctx, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            sql.append(statement)

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert (await execute(factory, case)).disposition == "succeeded"
    finally:
        event.remove(engine, "before_cursor_execute", record)
    expected = [
        "sys_user_field_policy_global_guard",
        "sys_user_field_policy_state",
        "ai_conversation",
        "ai_message",
        "ai_prepared_action",
        "ai_operation_log",
        "sys_role",
    ]
    assert all(
        name in statement for name, statement in zip(expected, sql[:7], strict=True)
    )
    assert "FOR UPDATE" in sql[1]


async def test_insufficient_lease_budget_never_starts_business(execution_case):
    factory, plain, case = execution_case
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                execution_lease_expires_at=datetime.now(UTC) + timedelta(seconds=9),
            )
        )
        await db.commit()
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert await db.scalar(select(Dept)) is None


async def rebind(db, case, tool, agent, args, business):
    snapshot = {"business": business, "argsHash": compute_args_hash(args)}
    await db.execute(
        update(AiPreparedAction).values(
            execute_tool_name=tool,
            agent_code=agent,
            frozen_args=args,
            args_hash=compute_args_hash(args),
            snapshot=snapshot,
            snapshot_hash=compute_args_hash(snapshot),
            tool_codes=[tool],
        )
    )
    await db.execute(
        update(AiOperationLog).values(
            tool_name=tool, agent_code=agent, args_hash=compute_args_hash(args)
        )
    )
    await db.commit()
    action = await db.scalar(
        select(AiPreparedAction).execution_options(populate_existing=True)
    )
    case.claim = ClaimBinding.from_action(action, case.log.log_id)


@pytest.mark.parametrize(
    "tool",
    [
        "dept.update",
        "dept.move",
        "role.create",
        "role.update",
        "role.update_menus",
        "user.update_dept",
        "user.update_roles",
    ],
)
async def test_adapted_tool_matrix(execution_case, tool):
    factory, plain, case = execution_case
    agent = (
        "dept_mgmt"
        if tool.startswith("dept.")
        else "role_mgmt"
        if tool.startswith("role.")
        else "user_mgmt"
    )
    async with plain() as db:
        for rid in (301, 302):
            await db.execute(
                insert(Role).values(
                    tenant_id=0,
                    role_id=rid,
                    role_name=f"Role {rid}",
                    role_code=f"R_{rid}",
                    data_scope="5",
                    status="1",
                )
            )
        for did in (401, 402):
            await db.execute(
                insert(Dept).values(
                    tenant_id=0,
                    dept_id=did,
                    dept_name=f"Dept {did}",
                    ancestors="0",
                    status="1",
                )
            )
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="target",
                hashed_password="unused",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=501, role_id=301)
        )
        await db.execute(update(AiAgent).values(code=agent))
        if tool == "dept.update":
            args = {"dept_id": 401, "dept_name": "Updated"}
            preview, *_ = await dept_service._build_preview(
                db,
                tenant=case.tenant,
                actor_user_id=101,
                action="update",
                dept_id=401,
                dept_in=DeptUpdate(dept_name="Updated"),
            )
        elif tool == "dept.move":
            args = {"dept_id": 401, "new_parent_id": 402}
            preview, *_ = await dept_service._build_preview(
                db,
                tenant=case.tenant,
                actor_user_id=101,
                action="move",
                dept_id=401,
                new_parent_id=402,
            )
        elif tool.startswith("role."):
            kwargs = {}
            if tool == "role.create":
                args = {
                    "role_name": "Created",
                    "role_code": "R_CREATED",
                    "data_scope": "5",
                    "status": "1",
                }
                kwargs["role_in"] = RoleCreate(**args)
            elif tool == "role.update":
                args = {"role_id": 301, "role_name": "Updated"}
                kwargs = {"role_id": 301, "role_in": RoleUpdate(role_name="Updated")}
            else:
                args = {"role_id": 301, "menu_ids": [1001]}
                kwargs = {"role_id": 301, "menu_ids": [1001]}
            evaluation = await role_management_service._evaluate(
                db,
                tenant=case.tenant,
                actor_user_id=101,
                action=tool.split(".")[1],
                **kwargs,
            )
            preview = evaluation.preview
        elif tool == "user.update_dept":
            args = {
                "user_id": 501,
                "dept_assignments": [{"dept_id": 401, "is_primary": True}],
            }
            preview = await user_department_assignment_service.preview_departments(
                db,
                tenant=case.tenant,
                actor_user_id=101,
                target_user_id=501,
                dept_assignments=[(401, True)],
            )
        else:
            args = {"user_id": 501, "role_ids": [302]}
            preview = await user_role_assignment_service.preview_roles(
                db,
                tenant=case.tenant,
                actor_user_id=101,
                target_user_id=501,
                role_ids=[302],
            )
        await rebind(db, case, tool, agent, args, preview.snapshot)
    assert (await execute(factory, case)).disposition == "succeeded"
    async with plain() as db:
        assert await epoch(db) == 2
        action = await db.scalar(select(AiPreparedAction))
        assert action.result_data and action.result_ui and action.subject_refs_hash
        if tool == "dept.move":
            assert (
                await db.scalar(select(Dept.parent_id).where(Dept.dept_id == 401))
                == 402
            )
        if tool == "user.update_roles":
            assert (
                await db.scalars(
                    select(user_roles.c.role_id).where(user_roles.c.user_id == 501)
                )
            ).all() == [302]


async def test_concurrent_confirmation_commits_once(execution_case):
    factory, plain, case = execution_case
    outcomes = await asyncio.gather(execute(factory, case), execute(factory, case))
    assert all(n.disposition == "succeeded" for n in outcomes)
    async with plain() as db:
        assert len((await db.scalars(select(Dept))).all()) == 1
        assert await epoch(db) == 2


async def test_context_set_change_is_rejected_without_late_locks(
    execution_case, monkeypatch
):

    factory, plain, case = execution_case
    discover = ai_context_candidate.discover
    added = False

    async def changed(*args, **kwargs):
        nonlocal added
        result = await discover(*args, **kwargs)
        if not added:
            added = True
            async with plain() as db:
                await db.execute(
                    insert(AiMessage).values(
                        tenant_id=0,
                        conversation_id=case.claim.conversation_id,
                        role="assistant",
                        content="new dependency",
                    )
                )
                await db.commit()
        return result

    monkeypatch.setattr(ai_context_candidate, "discover", changed)
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert await db.scalar(select(Dept)) is None
        assert await epoch(db) == 1


async def test_corrupt_claim_cannot_fence_original_action(execution_case):
    factory, plain, case = execution_case
    case.claim = replace(case.claim, snapshot_hash="0" * 64)
    assert (await execute(factory, case)).disposition == "unknown"
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "running"
        assert await db.scalar(select(Dept)) is None


async def test_stale_business_snapshot_rejected(execution_case):
    factory, plain, case = execution_case
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0,
                dept_id=401,
                dept_name="Changed dependency",
                ancestors="0",
                status="1",
            )
        )
        await db.commit()
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert (await db.scalars(select(Dept.dept_name))).all() == [
            "Changed dependency"
        ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("approval_mode", "auto"),
        ("dispatch_mode", "preview_only"),
        ("requested_outcome", "preview"),
    ],
)
async def test_non_execution_policy_cannot_use_claim(execution_case, field, value):
    factory, plain, case = execution_case
    async with plain() as db:
        await db.execute(update(AiPreparedAction).values(**{field: value}))
        await db.commit()
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert await db.scalar(select(Dept)) is None


@pytest.mark.parametrize("enabled", [False, True])
async def test_tool_allowlist_is_read_live_without_cached_authority(
    execution_case, monkeypatch, enabled
):
    factory, plain, case = execution_case
    registered = ToolRegistry.get().find("dept.create")
    monkeypatch.setattr(
        registered, "meta", replace(registered.meta, default_enabled=False)
    )
    # A process cache holding the opposite value must not influence the protected read.

    monkeypatch.setitem(
        ai_config._cache,
        (0, "ai:enabled_tools"),
        ([] if enabled else ["dept.create"], time.time()),
    )
    async with plain() as db:
        await db.execute(
            insert(SystemSetting).values(
                tenant_id=0,
                setting_key="ai:enabled_tools",
                setting_value='["dept.create"]' if enabled else "[]",
                status="1",
            )
        )
        # The settings mutation also changes the approval authority version.
        args = {"parent_id": None, "dept_name": "Gateway team", "status": "1"}
        preview, *_ = await dept_service._build_preview(
            db,
            tenant=case.tenant,
            action="create",
            actor_user_id=101,
            dept_in=DeptCreate(**args),
        )
        await rebind(db, case, "dept.create", "dept_mgmt", args, preview.snapshot)
    assert (await execute(factory, case)).disposition == (
        "succeeded" if enabled else "failed"
    )


async def test_unadapted_tool_never_falls_back_to_registered_handler(
    execution_case, monkeypatch
):
    factory, plain, case = execution_case
    calls = []

    async def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("must never invoke a raw handler")

    registered = ToolRegistry.get().find("role.update_agents")
    monkeypatch.setattr(registered, "fn", forbidden)
    async with plain() as db:
        await rebind(
            db,
            case,
            "role.update_agents",
            "role_mgmt",
            {"role_id": 301, "agent_ids": []},
            {},
        )
    assert (await execute(factory, case)).disposition == "failed"
    assert calls == []


async def test_revocation_waiting_on_barrier_wins_before_execution(execution_case):

    factory, plain, case = execution_case
    async with factory() as db:
        async with authorization_transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            key = ResourceKey(ResourceKind.USER, 0, 101)
            await tx.lock(LockPlan(existing=(key,)))
            async with tx.aggregate(0):
                await tx.update_held(key, {"status": "2"})
            task = asyncio.create_task(execute(factory, case))
            await asyncio.sleep(0.1)
            assert not task.done()
        await db.commit()
    assert (await task).disposition == "failed"
    async with plain() as db:
        assert await db.scalar(select(Dept)) is None


async def test_nested_grant_authority_ignores_stale_tool_cache(
    execution_case, monkeypatch
):

    factory, plain, case = execution_case
    for registered in ToolRegistry.get().by_agent("dept_mgmt"):
        monkeypatch.setattr(
            registered, "meta", replace(registered.meta, default_enabled=False)
        )
    monkeypatch.setitem(
        ai_config._cache, (0, "ai:enabled_tools"), (["dept.create"], time.time())
    )
    async with plain() as db:
        await db.execute(
            insert(SystemSetting).values(
                tenant_id=0,
                setting_key="ai:enabled_tools",
                setting_value="[]",
                status="1",
            )
        )
        await db.commit()
    async with factory() as db:
        async with authorization_transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            authority = await grant_authority_service.build(db, 101, tenant=case.tenant)
            assert authority.visible_agent_ids == frozenset()
            await tx.lock(LockPlan())
        await db.rollback()


async def test_prepared_domain_command_must_match_original_claim(
    execution_case, monkeypatch
):
    factory, plain, case = execution_case
    changed_args = {"parent_id": None, "dept_name": "Unapproved team", "status": "1"}
    async with plain() as db:
        preview, *_ = await dept_service._build_preview(
            db,
            tenant=case.tenant,
            actor_user_id=101,
            action="create",
            dept_in=DeptCreate(**changed_args),
        )
    prepare = system_ai_tool_adapter.prepare_input

    async def substitute(db, **kwargs):
        return await prepare(
            db, **(kwargs | {"args": changed_args, "snapshot": preview.snapshot})
        )

    monkeypatch.setattr(system_ai_tool_adapter, "prepare_input", substitute)
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert await db.scalar(select(Dept)) is None
        assert await epoch(db) == 1
