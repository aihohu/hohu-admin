"""Entrypoint retries release whole transactions before invoking business once."""

from collections.abc import Callable

import pytest
from sqlalchemy import delete, func, select

from app.core.exceptions import AuthorizationException
from app.db.base import user_roles
from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.ai.service.user_delete_dependency_service import (
    UserDeleteDependencyChanged,
    user_delete_dependency_service,
)
from app.modules.auth.service import load_live_user_authority
from app.modules.system.api import user as user_api
from app.modules.system.models.user import User
from tests.modules.ai.test_atomic_db_tools import (
    ToolCase,
)
from tests.modules.ai.test_atomic_db_tools import (
    db_tool_case as db_tool_case,
)


def inject_committed_dependency_changes(
    monkeypatch: pytest.MonkeyPatch,
    case: ToolCase,
    count: int,
    *,
    revoke_permission: bool = False,
) -> list[int]:
    """Insert actual FK children between context locks and final User locks."""
    transactions = []
    original = user_delete_dependency_service.lock_plan

    async def changed(db, plan, *, tenant):
        await original(db, plan, tenant=tenant)
        transactions.append(await db.scalar(select(func.txid_current())))
        if len(transactions) <= count:
            async with AsyncSessionLocal() as writer, writer.begin():
                writer.add(
                    AiConversation(
                        tenant_id=case.tenant.tenant_id,
                        user_id=case.target_user_id,
                        title=f"Entrypoint dependency change {len(transactions)}",
                    )
                )
                await writer.flush()
                if revoke_permission:
                    await writer.execute(
                        delete(user_roles).where(
                            user_roles.c.tenant_id == case.tenant.tenant_id,
                            user_roles.c.user_id == case.tenant.actor_user_id,
                        )
                    )

    monkeypatch.setattr(user_delete_dependency_service, "lock_plan", changed)
    return transactions


def count_real_delete_handler(
    monkeypatch: pytest.MonkeyPatch,
    case: ToolCase,
    *,
    after_handler: Callable[[], None] | None = None,
) -> None:
    registered = ToolRegistry.get().find(case.tool)
    assert registered is not None
    original = registered.fn

    async def counted(ctx, **args):
        case.calls.append(
            (
                await ctx.db.scalar(select(func.pg_backend_pid())),
                await ctx.db.scalar(select(func.txid_current())),
            )
        )
        result = await original(ctx, **args)
        assert result.ok, result.error_code
        if after_handler is not None:
            after_handler()
        return result

    monkeypatch.setattr(registered, "fn", counted)


async def approve(case: ToolCase):
    async with AsyncSessionLocal() as db:
        actor = await load_live_user_authority(db, tenant=case.tenant)
        return await confirm_module.confirm_tool(
            ConfirmRequest(confirmation_id=case.confirmation_id, action="approve"),
            db=db,
            current_user=actor,
        )


@pytest.mark.parametrize("db_tool_case", ["user.batch_delete"], indirect=True)
@pytest.mark.parametrize("changes", [1, 2])
async def test_ai_rebuilds_only_preflight_and_never_repeats_handler(
    db_tool_case: ToolCase, monkeypatch: pytest.MonkeyPatch, changes: int
) -> None:
    case = db_tool_case
    transactions = inject_committed_dependency_changes(monkeypatch, case, changes)
    count_real_delete_handler(monkeypatch, case)
    response = await approve(case)
    assert len(transactions) == 2
    assert len(set(transactions)) == 2, "A whole new transaction must rebuild locks"
    async with AsyncSessionLocal() as observer:
        target = await observer.get(User, case.target_user_id)
        action = await observer.get(AiPreparedAction, case.action_id)
        log = await observer.get(AiOperationLog, case.log_id)
        if changes == 1:
            assert target is None
            assert response.data.status == action.status == "succeeded"
            assert log.status == "success"
            assert len(case.calls) == 1
        else:
            assert target is not None
            assert response.data.status == action.status == log.status == "failed"
            assert (
                action.error_code == log.error_code == "USER_DELETE_DEPENDENCY_CHANGED"
            )
            assert action.execution_owner.startswith("v0162-db:")
            assert action.execution_lease_expires_at is None
            assert action.finished_at is not None and log.finished_at is not None
            assert case.calls == []
    await approve(case)
    assert len(case.calls) == (1 if changes == 1 else 0)


@pytest.mark.parametrize("db_tool_case", ["user.batch_delete"], indirect=True)
async def test_dependency_error_after_handler_start_rolls_back_without_retry(
    db_tool_case: ToolCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = db_tool_case

    def fail_after_delete():
        raise UserDeleteDependencyChanged()

    count_real_delete_handler(monkeypatch, case, after_handler=fail_after_delete)
    response = await approve(case)
    assert len(case.calls) == 1
    async with AsyncSessionLocal() as observer:
        assert await observer.get(User, case.target_user_id) is not None
        assert (
            await observer.get(AiConversation, case.target_conversation_id) is not None
        )
        action = await observer.get(AiPreparedAction, case.action_id)
        log = await observer.get(AiOperationLog, case.log_id)
        assert response.data.status == action.status == log.status == "failed"
    await approve(case)
    assert len(case.calls) == 1


async def http_delete(case: ToolCase, *, batch: bool):
    async with AsyncSessionLocal() as db:
        actor = await load_live_user_authority(db, tenant=case.tenant)
        if batch:
            return await user_api.batch_delete_users(
                [case.target_user_id], db=db, current_user=actor, tenant=case.tenant
            )
        return await user_api.delete_user(
            case.target_user_id, db=db, tenant=case.tenant
        )


@pytest.mark.parametrize("db_tool_case", ["user.batch_delete"], indirect=True)
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("changes", [1, 2])
async def test_http_rebuilds_at_most_once_after_whole_rollback(
    db_tool_case: ToolCase,
    monkeypatch: pytest.MonkeyPatch,
    batch: bool,
    changes: int,
) -> None:
    case = db_tool_case
    transactions = inject_committed_dependency_changes(monkeypatch, case, changes)
    if changes == 1:
        response = await http_delete(case, batch=batch)
        assert response.code == 200
    else:
        with pytest.raises(UserDeleteDependencyChanged):
            await http_delete(case, batch=batch)
    assert len(transactions) == 2
    assert len(set(transactions)) == 2
    async with AsyncSessionLocal() as observer:
        assert (await observer.get(User, case.target_user_id) is not None) == (
            changes == 2
        )


@pytest.mark.parametrize("db_tool_case", ["user.batch_delete"], indirect=True)
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_http_dependency_retry_rechecks_revoked_permission(
    db_tool_case: ToolCase, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    case = db_tool_case
    transactions = inject_committed_dependency_changes(
        monkeypatch, case, 1, revoke_permission=True
    )
    with pytest.raises(AuthorizationException):
        await http_delete(case, batch=batch)
    assert len(transactions) == 1, "Denied authority must not start a second delete"
    async with AsyncSessionLocal() as observer:
        assert await observer.get(User, case.target_user_id) is not None
        assert (
            await observer.get(AiConversation, case.target_conversation_id) is not None
        )
