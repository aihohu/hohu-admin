"""Real PostgreSQL deletion races with uniquely owned, committed fixtures.

Independent transactions are necessary for FK and context-lock races. The
fixture deletes only its exact users, conversations, actions and audit records.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import new_execution_owner
from app.modules.ai.service.prepared_action_service import canonical_payload_hash
from app.modules.ai.service.user_delete_dependency_service import (
    user_delete_dependency_service,
)
from app.modules.system.models.user import User
from app.modules.system.service.user_service import user_service


@dataclass
class DeleteCase:
    tenant: TenantContext
    user_id: int
    conversation_id: int
    source_id: int
    marker: str
    owned_user_ids: list[int] = field(default_factory=list)
    owned_action_ids: list[int] = field(default_factory=list)
    owned_log_ids: list[int] = field(default_factory=list)


@pytest.fixture
async def delete_case(db_session: AsyncSession) -> AsyncIterator[DeleteCase]:
    assert db_session.bind is not None
    assert settings.ENV == "test"
    marker = uuid4().hex[:12]
    async with AsyncSessionLocal() as db, db.begin():
        admin_id = await db.scalar(
            select(User.user_id).where(User.tenant_id == 0, User.user_name == "admin")
        )
        assert admin_id is not None, "Initialize the isolated test database first"
        target = User(
            tenant_id=0,
            user_name=f"del{marker}",
            hashed_password="unusable-test-fixture",
            status="1",
        )
        db.add(target)
        await db.flush()
        conversation = AiConversation(
            tenant_id=0, user_id=target.user_id, title=f"Delete race {marker}"
        )
        db.add(conversation)
        await db.flush()
        source = AiMessage(
            tenant_id=0,
            conversation_id=conversation.conversation_id,
            role="user",
            content="Uniquely owned deletion test",
            trace_id=f"delete_{marker}",
        )
        db.add(source)
        await db.flush()
        case = DeleteCase(
            tenant=TenantContext(0, "default", admin_id, 1, "access_token"),
            user_id=target.user_id,
            conversation_id=conversation.conversation_id,
            source_id=source.message_id,
            marker=marker,
            owned_user_ids=[target.user_id],
        )
    try:
        yield case
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(AiPreparedAction).where(
                    AiPreparedAction.action_id.in_(case.owned_action_ids)
                )
            )
            await db.execute(
                delete(AiOperationLog).where(
                    AiOperationLog.log_id.in_(case.owned_log_ids)
                )
            )
            for model in (AiPreparedAction, AiOperationLog, AiConversation, User):
                await db.execute(
                    delete(model).where(
                        model.tenant_id == 0, model.user_id.in_(case.owned_user_ids)
                    )
                )


async def add_dependency(
    case: DeleteCase,
    *,
    status: str = "running",
    with_action: bool = True,
    owner: str | None = None,
    deleted_source: bool = False,
) -> tuple[int | None, int]:
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db, db.begin():
        action = None
        if with_action:
            action = AiPreparedAction(
                tenant_id=0,
                user_id=case.user_id,
                conversation_id=case.conversation_id,
                source_user_message_id=case.source_id,
                confirmation_id=f"cid_delete_{case.marker}",
                execute_tool_call_id=f"tc_delete_{case.marker}",
                execute_tool_name="user.create",
                status=status,
                interaction_flow="direct",
                requested_outcome="direct",
                approval_mode="hitl",
                dispatch_mode="inline",
                frozen_args={},
                args_hash=canonical_payload_hash({}),
                snapshot={},
                snapshot_hash=canonical_payload_hash({}),
                presentation={},
                trace_id=f"delete_{case.marker}",
                agent_code="user_mgmt",
                expires_at=now + timedelta(minutes=5),
                approved_by=case.user_id,
                approved_at=now,
                execution_owner=owner,
                execution_lease_expires_at=now - timedelta(seconds=5),
                finished_at=now if status == "failed" else None,
            )
            db.add(action)
        log = AiOperationLog(
            tenant_id=0,
            user_id=case.user_id,
            conversation_id=case.conversation_id,
            source_user_message_id=case.source_id,
            readonly_snapshot=False,
            trace_id=f"delete_{case.marker}",
            agent_code="user_mgmt",
            tool_name="user.create",
            tool_call_id=f"tc_delete_{case.marker}",
            args_hash=canonical_payload_hash({}),
            args_summary="Uniquely owned deletion fixture",
            risk_level="high",
            execution_mode="hitl",
            status=status,
            confirmation_id=f"cid_delete_{case.marker}" if with_action else None,
            approved_by=case.user_id,
            started_at=now.replace(tzinfo=None),
            finished_at=now.replace(tzinfo=None) if status == "failed" else None,
        )
        db.add(log)
        if deleted_source:
            conversation = await db.get(AiConversation, case.conversation_id)
            source = await db.get(AiMessage, case.source_id)
            conversation.deleted_at = now
            source.is_active = False
        await db.flush()
        if action is not None:
            case.owned_action_ids.append(action.action_id)
        case.owned_log_ids.append(log.log_id)
        return action.action_id if action else None, log.log_id


async def delete_target(case: DeleteCase, *, batch: bool) -> BusinessException | None:
    async with AsyncSessionLocal() as db:
        try:
            if batch:
                await user_service.batch_delete_users(
                    db, [case.user_id], case.tenant.actor_user_id, tenant=case.tenant
                )
            else:
                await user_service.delete_user(db, case.user_id, tenant=case.tenant)
            await db.commit()
        except BusinessException as exc:
            await db.rollback()
            return exc
    return None


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize(
    ("status", "with_action", "deleted_source"),
    [
        ("running", True, False),
        ("running", True, True),
        ("running", False, False),
        ("failed", True, False),
        ("failed", False, False),
        ("pending_confirmation", True, False),
    ],
)
async def test_hard_delete_preserves_unresolved_write_evidence(
    delete_case: DeleteCase,
    batch: bool,
    status: str,
    with_action: bool,
    deleted_source: bool,
) -> None:
    case = delete_case
    await add_dependency(
        case,
        status=status,
        with_action=with_action,
        owner=new_execution_owner("user.create") if status == "running" else None,
        deleted_source=deleted_source,
    )
    error = await delete_target(case, batch=batch)
    async with AsyncSessionLocal() as observer:
        user = await observer.get(User, case.user_id)
        conversation = await observer.get(AiConversation, case.conversation_id)
        source = await observer.get(AiMessage, case.source_id)
    assert user is not None, "Hard deletion erased a user with unresolved AI writes"
    assert conversation is not None and source is not None
    assert error is not None
    assert error.error_code == "USER_AI_DEPENDENCY_UNRESOLVED"


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_hard_delete_without_write_dependencies_still_succeeds(
    delete_case: DeleteCase, batch: bool
) -> None:
    case = delete_case
    assert await delete_target(case, batch=batch) is None
    async with AsyncSessionLocal() as observer:
        assert await observer.get(User, case.user_id) is None
        assert await observer.get(AiConversation, case.conversation_id) is None
        assert await observer.get(AiMessage, case.source_id) is None


async def wait_for_database_lock(pid: int) -> None:
    """Observe PostgreSQL blocking, never infer a race from an elapsed sleep."""
    async with asyncio.timeout(1.5), AsyncSessionLocal() as observer:
        while True:
            blocking = await observer.scalar(select(func.pg_blocking_pids(pid)))
            if blocking:
                return


async def finish_success(db: AsyncSession, action_id: int, log_id: int) -> None:
    action = await db.get(AiPreparedAction, action_id)
    log = await db.get(AiOperationLog, log_id)
    action.status = "succeeded"
    action.row_version += 1
    action.execution_owner = None
    action.execution_lease_expires_at = None
    action.finished_at = datetime.now(UTC)
    log.status = "success"
    log.finished_at = datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_new_conversation_after_plan_requires_whole_transaction_restart(
    delete_case: DeleteCase, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    case = delete_case
    planned = asyncio.Event()
    inserted = asyncio.Event()
    original = user_delete_dependency_service.lock_plan

    async def pause_after_context_locks(db, plan, *, tenant):
        await original(db, plan, tenant=tenant)
        planned.set()
        await inserted.wait()

    monkeypatch.setattr(
        user_delete_dependency_service, "lock_plan", pause_after_context_locks
    )
    deletion = asyncio.create_task(delete_target(case, batch=batch))
    try:
        await asyncio.wait_for(planned.wait(), 3)
        async with AsyncSessionLocal() as creator, creator.begin():
            assert (
                await creator.scalar(
                    select(func.current_setting("transaction_isolation"))
                )
                == "read committed"
            )
            creator.add(
                AiConversation(
                    tenant_id=0, user_id=case.user_id, title="Concurrent FK insertion"
                )
            )
            await creator.flush()
        inserted.set()
        error = await asyncio.wait_for(deletion, 3)
        assert error is not None
        assert error.error_code == "USER_DELETE_DEPENDENCY_CHANGED"
        async with AsyncSessionLocal() as observer:
            assert await observer.get(User, case.user_id) is not None
            assert (
                await observer.scalar(
                    select(func.count())
                    .select_from(AiConversation)
                    .where(
                        AiConversation.tenant_id == 0,
                        AiConversation.user_id == case.user_id,
                    )
                )
                == 2
            )
        monkeypatch.setattr(user_delete_dependency_service, "lock_plan", original)
        assert await delete_target(case, batch=batch) is None
    finally:
        inserted.set()
        await asyncio.gather(deletion, return_exceptions=True)


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_user_row_lock_fences_new_conversation_fk_insert(
    delete_case: DeleteCase, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    case = delete_case
    user_locked = asyncio.Event()
    allow_delete = asyncio.Event()
    creator_started = asyncio.Event()
    creator_pid: list[int] = []
    original = user_delete_dependency_service.recheck_after_user_locks

    async def pause_after_user_lock(db, plan, *, tenant):
        await original(db, plan, tenant=tenant)
        user_locked.set()
        await allow_delete.wait()

    async def create_conversation():
        async with AsyncSessionLocal() as creator, creator.begin():
            creator_pid.append(await creator.scalar(select(func.pg_backend_pid())))
            creator.add(
                AiConversation(tenant_id=0, user_id=case.user_id, title="Blocked FK")
            )
            creator_started.set()
            await creator.flush()

    monkeypatch.setattr(
        user_delete_dependency_service,
        "recheck_after_user_locks",
        pause_after_user_lock,
    )
    deletion = asyncio.create_task(delete_target(case, batch=batch))
    insertion = None
    try:
        await asyncio.wait_for(user_locked.wait(), 3)
        insertion = asyncio.create_task(create_conversation())
        await asyncio.wait_for(creator_started.wait(), 3)
        await wait_for_database_lock(creator_pid[0])
        assert not insertion.done()
        allow_delete.set()
        assert await asyncio.wait_for(deletion, 3) is None
        with pytest.raises(IntegrityError):
            await asyncio.wait_for(insertion, 3)
        async with AsyncSessionLocal() as observer:
            assert await observer.get(User, case.user_id) is None
            assert (
                await observer.scalar(
                    select(func.count())
                    .select_from(AiConversation)
                    .where(
                        AiConversation.tenant_id == 0,
                        AiConversation.user_id == case.user_id,
                    )
                )
                == 0
            )
    finally:
        allow_delete.set()
        await asyncio.gather(
            *([deletion, insertion] if insertion is not None else [deletion]),
            return_exceptions=True,
        )


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_execution_context_winner_can_lock_user_and_commit_before_delete(
    delete_case: DeleteCase, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    case = delete_case
    action_id, log_id = await add_dependency(
        case, owner=new_execution_owner("user.create")
    )
    entered = asyncio.Event()
    delete_pid: list[int] = []
    original = user_delete_dependency_service.lock_plan

    async def observe_deletion(db, plan, *, tenant):
        delete_pid.append(await db.scalar(select(func.pg_backend_pid())))
        entered.set()
        await original(db, plan, tenant=tenant)

    monkeypatch.setattr(user_delete_dependency_service, "lock_plan", observe_deletion)
    deletion = None
    async with AsyncSessionLocal() as executor:
        try:
            await executor.execute(
                select(AiConversation)
                .where(AiConversation.conversation_id == case.conversation_id)
                .with_for_update()
            )
            deletion = asyncio.create_task(delete_target(case, batch=batch))
            await asyncio.wait_for(entered.wait(), 3)
            await wait_for_database_lock(delete_pid[0])
            # An old User-first cascade holds this lock and deadlocks against
            # our context. The new delete must be waiting before taking User.
            await executor.execute(text("SET LOCAL lock_timeout = '1s'"))
            await executor.execute(
                select(User).where(User.user_id == case.user_id).with_for_update()
            )
            await finish_success(executor, action_id, log_id)
            await executor.commit()
            assert await asyncio.wait_for(deletion, 3) is None
            async with AsyncSessionLocal() as observer:
                assert await observer.get(User, case.user_id) is None
                assert (await observer.get(AiPreparedAction, action_id)).status == (
                    "succeeded"
                )
                assert (await observer.get(AiOperationLog, log_id)).status == "success"
        finally:
            await executor.rollback()
            if deletion is not None:
                await asyncio.gather(deletion, return_exceptions=True)


async def test_definite_db_failure_can_delete_but_file_failure_cannot(
    delete_case: DeleteCase,
) -> None:
    case = delete_case
    action_id, log_id = await add_dependency(
        case, status="failed", owner=new_execution_owner("user.create")
    )
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, action_id)
        log = await db.get(AiOperationLog, log_id)
        action.execution_lease_expires_at = None
        action.error_code = log.error_code = "TEST_ROLLED_BACK"
        action.execute_tool_name = log.tool_name = "user.export"
        action.execution_owner = new_execution_owner("user.export")
    assert (await delete_target(case, batch=True)).error_code == (
        "USER_AI_DEPENDENCY_UNRESOLVED"
    )
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, action_id)
        log = await db.get(AiOperationLog, log_id)
        action.execute_tool_name = log.tool_name = "user.create"
        action.execution_owner = new_execution_owner("user.create")
    assert await delete_target(case, batch=True) is None


async def test_locatable_orphan_write_log_preserves_user(
    delete_case: DeleteCase,
) -> None:
    case = delete_case
    _, log_id = await add_dependency(case, with_action=False)
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            delete(AiConversation).where(
                AiConversation.conversation_id == case.conversation_id
            )
        )
    error = await delete_target(case, batch=True)
    assert error is not None
    assert error.error_code == "USER_AI_DEPENDENCY_UNRESOLVED"
    async with AsyncSessionLocal() as observer:
        assert await observer.get(User, case.user_id) is not None
        assert await observer.get(AiOperationLog, log_id) is not None


@pytest.mark.parametrize(
    "broken_fact", ["missing_log", "log_args", "source", "principal", "annotation"]
)
async def test_raw_success_without_complete_proof_cannot_unlock_deletion(
    delete_case: DeleteCase, broken_fact: str
) -> None:
    case = delete_case
    action_id, log_id = await add_dependency(
        case, owner=new_execution_owner("user.create")
    )
    async with AsyncSessionLocal() as db, db.begin():
        await finish_success(db, action_id, log_id)
        log = await db.get(AiOperationLog, log_id)
        if broken_fact == "missing_log":
            await db.delete(log)
        elif broken_fact == "log_args":
            log.args_hash = "c" * 64
        elif broken_fact == "source":
            await db.execute(
                delete(AiMessage).where(AiMessage.message_id == case.source_id)
            )
        elif broken_fact == "principal":
            log.user_id = case.tenant.actor_user_id
        else:
            log.result_summary = '{"__v0162_reconciliation_v1": damaged'
    error = await delete_target(case, batch=True)
    assert error is not None
    assert error.error_code == "USER_AI_DEPENDENCY_UNRESOLVED"
    async with AsyncSessionLocal() as observer:
        assert await observer.get(User, case.user_id) is not None


async def test_complete_success_with_hidden_source_remains_deletable(
    delete_case: DeleteCase,
) -> None:
    case = delete_case
    action_id, log_id = await add_dependency(
        case, owner=new_execution_owner("user.create"), deleted_source=True
    )
    async with AsyncSessionLocal() as db, db.begin():
        await finish_success(db, action_id, log_id)
    assert await delete_target(case, batch=True) is None


async def test_delete_rejects_snapshot_isolation(delete_case: DeleteCase) -> None:
    case = delete_case
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        with pytest.raises(BusinessException) as failure:
            await user_service.delete_user(db, case.user_id, tenant=case.tenant)
        assert failure.value.error_code == "USER_DELETE_ISOLATION_UNSUPPORTED"
        await db.rollback()
    async with AsyncSessionLocal() as observer:
        assert await observer.get(User, case.user_id) is not None


async def test_inverse_target_orders_use_the_same_lock_layers_and_converge(
    delete_case: DeleteCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = delete_case
    async with AsyncSessionLocal() as db, db.begin():
        target = User(
            tenant_id=0,
            user_name=f"d2{case.marker}",
            hashed_password="unusable-test-fixture",
            status="1",
        )
        db.add(target)
        await db.flush()
        case.owned_user_ids.append(target.user_id)
        conversation = AiConversation(
            tenant_id=0, user_id=target.user_id, title="Second"
        )
        db.add(conversation)
        await db.flush()
        source = AiMessage(
            tenant_id=0,
            conversation_id=conversation.conversation_id,
            role="user",
            content="Second source",
            trace_id=f"delete_second_{case.marker}",
        )
        db.add(source)
        await db.flush()
        other = DeleteCase(
            case.tenant,
            target.user_id,
            conversation.conversation_id,
            source.message_id,
            f"second_{case.marker}",
            case.owned_user_ids,
            case.owned_action_ids,
            case.owned_log_ids,
        )
    for current in (case, other):
        action_id, log_id = await add_dependency(
            current, owner=new_execution_owner("user.create")
        )
        async with AsyncSessionLocal() as db, db.begin():
            await finish_success(db, action_id, log_id)
    all_planned = asyncio.Event()
    plans = []
    original = user_delete_dependency_service.lock_plan
    locked_sql: dict[str, list[str]] = {"forward": [], "reverse": []}

    async def synchronize_plans(db, plan, *, tenant):
        plans.append(plan)
        if len(plans) == 2:
            all_planned.set()
        await all_planned.wait()
        await original(db, plan, tenant=tenant)

    def capture_locks(connection, _cursor, statement, _params, context, _many):
        label = connection.info.get("delete_test_order")
        if label not in locked_sql or "FOR UPDATE" not in statement:
            return
        compiled = context.compiled.statement
        table_name = compiled.get_final_froms()[0].name
        locked_sql[label].append(table_name)
        order_columns = list(compiled._order_by_clauses)
        assert len(order_columns) == 1, statement
        column = order_columns[0]
        values = [
            value
            for key, value in context.compiled_parameters[0].items()
            if key.startswith(f"{column.key}_")
        ]
        assert values == sorted(values), (statement, values)

    async def remove(label, targets):
        async with AsyncSessionLocal() as db:
            connection = await db.connection()
            connection_info = connection.sync_connection.info
            connection_info["delete_test_order"] = label
            try:
                await user_service.batch_delete_users(
                    db, targets, case.tenant.actor_user_id, tenant=case.tenant
                )
                await db.commit()
                return None
            except BusinessException as exc:
                await db.rollback()
                return exc.error_code
            finally:
                connection_info.pop("delete_test_order", None)

    monkeypatch.setattr(user_delete_dependency_service, "lock_plan", synchronize_plans)
    event.listen(engine.sync_engine, "before_cursor_execute", capture_locks)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                remove("forward", [case.user_id, other.user_id]),
                remove("reverse", [other.user_id, case.user_id]),
            ),
            5,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_locks)
    assert results.count(None) == 1
    assert results.count("USER_DELETE_DEPENDENCY_CHANGED") == 1
    layers = ["ai_conversation", "ai_message", "ai_prepared_action", "ai_operation_log"]
    assert locked_sql["forward"][:4] == layers
    assert locked_sql["reverse"][:4] == layers
    for actual in locked_sql.values():
        assert actual in (layers, [*layers, "sys_user"])
    assert plans[0] == plans[1]
    async with AsyncSessionLocal() as observer:
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(User)
                .where(User.user_id.in_(case.owned_user_ids))
            )
            == 0
        )
