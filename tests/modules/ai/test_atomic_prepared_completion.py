"""Real DB invariants for prepared business completion and optional projection.

Independent sessions are necessary: a request rollback must not hide a durable
business/receipt split from the observer. Only uniquely owned rows are committed;
the fixture deletes those exact rows even when an invariant fails.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import IS_PRIMARY_YES
from app.core import redis as redis_module
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.db.base import user_depts, user_roles
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.agents.gateway import atomic_execution
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.hitl.constants import AiOperationStatus
from app.modules.ai.agents.tools import load_builtin_tools
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.ai.service.chat_run_service import chat_run_finalizer
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import (
    canonical_payload_hash,
    prepared_action_service,
)
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import load_live_user_authority
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User


class InjectedCompletionFailure(RuntimeError):
    """A named logical boundary, never an ordinal commit call, failed."""


@dataclass
class CompletionCase:
    username: str
    confirmation_id: str
    tool_call_id: str
    tenant: TenantContext
    department_id: int
    default_role_id: int
    conversation_id: int
    provider_id: int
    action_id: int
    log_id: int
    args: dict[str, Any]
    calls: list[tuple[int, int]] = field(default_factory=list)
    created_user_ids: list[int] = field(default_factory=list)
    handler_errors: list[str] = field(default_factory=list)


@pytest.fixture
async def completion_case(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[CompletionCase]:
    # Reuse the module fixture's loop-local Redis/engine lifecycle. Its outer
    # transaction cannot contain writes that the real gateway must see through
    # another connection, so this fixture owns and cleans committed setup below.
    assert db_session.bind is not None
    assert settings.ENV == "test"
    monkeypatch.setattr(confirm_module, "redis_client", redis_module.redis_client)
    marker = uuid4().hex[:12]
    username = f"atx{marker}"
    confirmation_id = f"cid_atomic_{marker}"
    trace_id = f"tr_atomic_{marker}"
    tool_call_id = f"tc_atomic_{marker}"
    load_builtin_tools()
    async with AsyncSessionLocal() as db, db.begin():
        await ToolRegistry.get().validate_on_startup(db)
        admin_id = await db.scalar(
            select(User.user_id).where(User.tenant_id == 0, User.user_name == "admin")
        )
        assert admin_id is not None, "Initialize the isolated test database first"
        tenant = TenantContext(
            tenant_id=0,
            tenant_code="default",
            actor_user_id=admin_id,
            tenant_version=1,
            source="access_token",
        )
        await load_live_user_authority(db, tenant=tenant)
        default_role_id = await db.scalar(
            select(Role.role_id).where(Role.tenant_id == 0, Role.role_code == "R_USER")
        )
        assert default_role_id is not None
        department = Dept(
            tenant_id=0, dept_name=f"Atomic {marker}", ancestors="0", status="1"
        )
        provider = AiProvider(
            provider_code=f"atomic_{marker}",
            name="No-network completion fixture",
            api_key="unusable-test-fixture",
            base_url="https://invalid.example.invalid",
            is_enabled=True,
        )
        conversation = AiConversation(
            tenant_id=0,
            user_id=admin_id,
            title=f"Atomic {marker}",
            agent_code="user_mgmt",
            trace_id=trace_id,
        )
        db.add_all([department, provider, conversation])
        await db.flush()
        model = AiModel(
            provider_id=provider.provider_id,
            name=f"atomic-{marker}",
            capabilities={"text": True, "tool_calling": True},
            is_enabled=True,
        )
        message = AiMessage(
            tenant_id=0,
            conversation_id=conversation.conversation_id,
            role="user",
            content="Create the uniquely owned completion regression user",
            trace_id=trace_id,
            is_active=True,
        )
        db.add_all([model, message])
        await db.flush()
        args = {
            "user_name": username,
            "primary_dept_id": department.dept_id,
            "role_assignment": "DEFAULT",
        }
        action = await prepared_action_service.create_pending(
            db,
            confirmation_id=confirmation_id,
            prepare_tool_call_id=None,
            prepare_tool_name=None,
            execute_tool_call_id=tool_call_id,
            execute_tool_name="user.create",
            frozen_args=args,
            snapshot={
                "tool": "user.create",
                "argsHash": canonical_payload_hash(args),
                "dryRun": None,
            },
            snapshot_hash="",
            subject_ref=None,
            presentation={
                "title": "user.create",
                "fields": [{"label": "user_name", "value": username}],
            },
            interaction_flow="direct",
            requested_outcome="direct",
            user_id=admin_id,
            tenant=tenant,
            conversation_id=conversation.conversation_id,
            source_user_message_id=message.message_id,
            trace_id=trace_id,
            agent_code="user_mgmt",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            resolved_model_id=model.model_id,
            resolved_provider_id=provider.provider_id,
            require_live_source=True,
        )
        log_id = await operation_log_service.start_operation(
            db,
            trace_id=trace_id,
            conversation_id=conversation.conversation_id,
            tenant=tenant,
            source_user_message_id=message.message_id,
            readonly_snapshot=False,
            agent_code="user_mgmt",
            user_id=admin_id,
            tool_name="user.create",
            tool_call_id=tool_call_id,
            args_hash=action.args_hash,
            args_summary="user.create high hitl; uniquely owned regression fixture",
            risk_level="high",
            execution_mode="hitl",
            status=AiOperationStatus.PENDING_CONFIRMATION,
            confirmation_id=confirmation_id,
        )
        case = CompletionCase(
            username=username,
            confirmation_id=confirmation_id,
            tool_call_id=tool_call_id,
            tenant=tenant,
            department_id=department.dept_id,
            default_role_id=default_role_id,
            conversation_id=conversation.conversation_id,
            provider_id=provider.provider_id,
            action_id=action.action_id,
            log_id=log_id,
            args=args,
        )
    try:
        yield case
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            for model_type, condition in (
                (AiPreparedAction, AiPreparedAction.action_id == case.action_id),
                (AiOperationLog, AiOperationLog.log_id == case.log_id),
                (
                    AiConversation,
                    AiConversation.conversation_id == case.conversation_id,
                ),
                (User, User.user_name == case.username),
                (Dept, Dept.dept_id == case.department_id),
            ):
                await db.execute(
                    delete(model_type).where(model_type.tenant_id == 0, condition)
                )
            await db.execute(
                delete(AiProvider).where(AiProvider.provider_id == case.provider_id)
            )
        await redis_module.redis_client.delete(
            f"ai:tenant:0:failures:{tenant.actor_user_id}:user.create:"
            f"{compute_args_hash(case.args)}"
        )


def count_real_handler(
    case: CompletionCase,
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_failure: bool = False,
    unserializable_result: bool = False,
    scope_bound_result: bool = False,
) -> None:
    registered = ToolRegistry.get().find("user.create")
    assert registered is not None
    real_tool = registered.fn

    async def counted(ctx, **args):
        case.calls.append(
            (
                await ctx.db.scalar(select(func.pg_backend_pid())),
                await ctx.db.scalar(select(func.txid_current())),
            )
        )
        try:
            result = await real_tool(ctx, **args)
        except Exception as exc:
            case.handler_errors.append(
                f"{type(exc).__name__}: {getattr(exc, 'error_code', None)}"
            )
            raise
        assert result.ok, result.error_code
        user_id = await ctx.db.scalar(
            select(User.user_id).where(
                User.tenant_id == 0, User.user_name == case.username
            )
        )
        assert user_id is not None
        case.created_user_ids.append(user_id)
        if return_failure:
            return ToolResult.failure(
                error_code="TEST_HANDLER_FAILURE",
                error_msg="Failure after actual write",
            )
        if unserializable_result:
            result.data = {**result.data, "serializationProbe": object()}
        if scope_bound_result:
            result.projection = replace(result.projection, scope_bound=True)
        return result

    monkeypatch.setattr(registered, "fn", counted)


async def approve(case: CompletionCase):
    async with AsyncSessionLocal() as db:
        actor = await load_live_user_authority(db, tenant=case.tenant)
        try:
            return await confirm_module.confirm_tool(
                ConfirmRequest(confirmation_id=case.confirmation_id, action="approve"),
                db=db,
                current_user=actor,
            )
        except InjectedCompletionFailure:
            # Match request-dependency rollback after an unhandled failure.
            await db.rollback()
            return None


async def observe(case: CompletionCase) -> dict[str, Any]:
    """Read only committed records in a new session, not the request identity map."""
    async with AsyncSessionLocal() as db:
        user_ids = list(
            await db.scalars(
                select(User.user_id).where(
                    User.tenant_id == 0, User.user_name == case.username
                )
            )
        )
        role_ids = list(
            await db.scalars(
                select(user_roles.c.role_id).where(
                    user_roles.c.tenant_id == 0,
                    user_roles.c.user_id.in_(case.created_user_ids),
                )
            )
        )
        department_rows = (
            await db.execute(
                select(user_depts.c.dept_id, user_depts.c.is_primary).where(
                    user_depts.c.tenant_id == 0,
                    user_depts.c.user_id.in_(case.created_user_ids),
                )
            )
        ).all()
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        assert action is not None and log is not None
        history = list(
            await db.scalars(
                select(AiMessage.tool_calls)
                .where(
                    AiMessage.tenant_id == 0,
                    AiMessage.conversation_id == case.conversation_id,
                    AiMessage.role == "assistant",
                    AiMessage.is_active.is_(True),
                )
                .order_by(AiMessage.message_id)
            )
        )
        return {
            "user_ids": user_ids,
            "role_ids": role_ids,
            "department_rows": department_rows,
            "action_status": action.status,
            "result_data": action.result_data,
            "data_scope_hash": action.data_scope_hash,
            "operation_status": log.status,
            "history": history,
        }


def assert_success(case: CompletionCase, state: dict[str, Any]) -> None:
    assert case.handler_errors == []
    assert len(case.calls) == 1
    assert state["user_ids"] == case.created_user_ids
    assert len(state["user_ids"]) == 1
    assert state["role_ids"] == [case.default_role_id]
    assert state["department_rows"] == [(case.department_id, IS_PRIMARY_YES)]
    assert state["action_status"] == "succeeded"
    assert state["result_data"]["created"] == 1
    assert state["operation_status"] == "success"


async def test_projection_failure_keeps_committed_business_action_and_audit(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    real_finalize = chat_run_finalizer.finalize_prepared_action
    projection_hits = []

    async def fail_projection(*args, **kwargs):
        await real_finalize(*args, **kwargs)
        projection_hits.append(True)
        raise InjectedCompletionFailure("chat projection failed after actual flush")

    monkeypatch.setattr(chat_run_finalizer, "finalize_prepared_action", fail_projection)
    await approve(case)
    assert projection_hits == [True]
    state = await observe(case)
    assert_success(case, state)
    assert state["history"] == []

    monkeypatch.setattr(chat_run_finalizer, "finalize_prepared_action", real_finalize)
    response = await approve(case)
    assert response.data.status == "succeeded"
    assert (await approve(case)).data.status == "succeeded"
    state = await observe(case)
    assert_success(case, state)
    assert len(state["history"]) == 1
    cards = state["history"][0]
    assert len(cards) == 1
    assert cards[0]["tool_call_id"] == case.tool_call_id
    assert cards[0]["ok"] is True


async def test_repeat_confirmation_runs_real_handler_once(completion_case, monkeypatch):
    case = completion_case
    count_real_handler(case, monkeypatch)
    response = await approve(case)
    assert case.handler_errors == []
    assert response.data.status == "succeeded"
    assert (await approve(case)).data.status == "succeeded"
    assert_success(case, await observe(case))


async def test_required_success_audit_sql_failure_rolls_back_business(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    audit_hits = []

    def fail_success_update(_conn, _cursor, _sql, _params, context, _executemany):
        compiled = context.compiled
        statement = getattr(compiled, "statement", None)
        if not getattr(statement, "is_update", False):
            return
        if statement.table.name != "ai_operation_log":
            return
        for parameters in context.compiled_parameters:
            if (
                parameters.get("status") == "success"
                and case.log_id in parameters.values()
            ):
                audit_hits.append(True)
                raise InjectedCompletionFailure("required success audit SQL rejected")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_success_update)
    try:
        await approve(case)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_success_update)
    assert audit_hits, "The real success audit SQL must reach the failure hook"
    assert len(case.calls) == len(case.created_user_ids) == 1
    state = await observe(case)
    assert state["user_ids"] == []
    assert state["role_ids"] == []
    assert state["department_rows"] == []
    assert state["action_status"] != "succeeded"
    assert state["result_data"] is None
    assert state["operation_status"] != "success"


async def test_handler_failure_result_rolls_back_actual_writes(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=True)
    response = await approve(case)
    assert response.data.status == "failed"
    assert len(case.calls) == len(case.created_user_ids) == 1
    state = await observe(case)
    assert state["user_ids"] == []
    assert state["role_ids"] == []
    assert state["department_rows"] == []
    assert state["action_status"] == "failed"
    assert state["result_data"] is None
    assert state["operation_status"] == "failed"


@pytest.mark.parametrize(
    "broken_binding",
    ["missing", "trace_id", "source_user_message_id", "agent_code", "args_hash"],
)
async def test_required_log_binding_rejects_before_handler(
    completion_case, monkeypatch, broken_binding
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    async with AsyncSessionLocal() as db, db.begin():
        if broken_binding == "missing":
            await db.execute(
                delete(AiOperationLog).where(AiOperationLog.log_id == case.log_id)
            )
        else:
            replacements = {
                "trace_id": "tr_unrelated_atomic_fixture",
                "source_user_message_id": -1,
                "agent_code": "role_mgmt",
                "args_hash": "0" * 64,
            }
            await db.execute(
                update(AiOperationLog)
                .where(AiOperationLog.log_id == case.log_id)
                .values(**{broken_binding: replacements[broken_binding]})
            )

    try:
        response = await approve(case)
    except BusinessException:
        # Either a domain rejection or a fail-closed result is acceptable; the
        # missing/misbound audit must never authorize entry into the handler.
        response = None
    assert case.calls == []
    assert case.created_user_ids == []
    if response is not None:
        assert response.data.status != "succeeded"
    async with AsyncSessionLocal() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.tenant_id == 0, User.user_name == case.username)
            )
            == 0
        )
        action = await db.get(AiPreparedAction, case.action_id)
        assert action.status != "succeeded"
        assert action.result_data is None
        log = await db.get(AiOperationLog, case.log_id)
        if broken_binding == "missing":
            assert log is None
        else:
            assert log.status != "success"


async def test_terminal_pa_cas_zero_rows_rolls_back_business(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    cas_hits = []

    def miss_terminal_cas(_conn, _cursor, sql, parameters, context, _executemany):
        compiled = context.compiled
        statement = getattr(compiled, "statement", None)
        if (
            not getattr(statement, "is_update", False)
            or statement.table.name != "ai_prepared_action"
        ):
            return sql, parameters
        values = context.compiled_parameters[0]
        if values.get("status") != "succeeded":
            return sql, parameters
        identity_binds = [
            key
            for key, value in values.items()
            if "action_id" in key and value == case.action_id
        ]
        if not identity_binds:
            return sql, parameters
        # Make the actual UPDATE match zero rows. This tests the SQL result,
        # rather than replacing the service's return value with a mock None.
        replacement = list(parameters)
        for key in identity_binds:
            replacement[compiled.positiontup.index(key)] = -1
        cas_hits.append(True)
        return sql, tuple(replacement)

    event.listen(
        engine.sync_engine, "before_cursor_execute", miss_terminal_cas, retval=True
    )
    try:
        await approve(case)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", miss_terminal_cas)
    assert cas_hits, "The real PA terminal UPDATE must reach a zero-row CAS"
    assert len(case.calls) == len(case.created_user_ids) == 1
    state = await observe(case)
    assert state["user_ids"] == []
    assert state["role_ids"] == []
    assert state["department_rows"] == []
    assert state["action_status"] != "succeeded"
    assert state["operation_status"] != "success"
    assert state["result_data"] is None


async def test_result_json_serialization_failure_rolls_back_business(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch, unserializable_result=True)
    try:
        await approve(case)
    except StatementError as exc:
        assert isinstance(exc.orig, TypeError)
    except TypeError:
        # JSON validation may run before SQLAlchemy binds the JSONB column.
        pass
    assert len(case.calls) == len(case.created_user_ids) == 1
    assert case.handler_errors == []
    state = await observe(case)
    assert state["user_ids"] == []
    assert state["role_ids"] == []
    assert state["department_rows"] == []
    assert state["action_status"] != "succeeded"
    assert state["operation_status"] != "success"
    assert state["result_data"] is None


async def test_redis_success_counter_failure_cannot_undo_business(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    redis_observations = []

    async def fail_counter_clear(*_args, **_kwargs):
        redis_observations.append(await observe(case))
        raise RedisConnectionError("synthetic counter cleanup connection failure")

    with monkeypatch.context() as redis_patch:
        redis_patch.setattr(atomic_execution, "clear_failures", fail_counter_clear)
        response = await approve(case)
    assert len(redis_observations) == 1
    # The outcome must already be visible through another session when this
    # optional bookkeeping begins, not merely after the request returns.
    assert_success(case, redis_observations[0])
    assert response.data.status == "succeeded"
    assert_success(case, await observe(case))


async def test_scope_bound_result_freezes_hash_in_post_write_transaction(
    completion_case, monkeypatch
):
    """Retain the old confirm scope contract at its new real transaction owner."""
    case = completion_case
    count_real_handler(case, monkeypatch, scope_bound_result=True)
    real_hash = result_projection_service.compute_data_scope_hash
    scope_observations = []

    async def observed_hash(db, user, **kwargs):
        value = await real_hash(db, user, **kwargs)
        transaction_id = await db.scalar(select(func.txid_current()))
        user_id = await db.scalar(
            select(User.user_id).where(
                User.tenant_id == 0, User.user_name == case.username
            )
        )
        scope_observations.append((transaction_id, user_id, value))
        return value

    monkeypatch.setattr(
        result_projection_service, "compute_data_scope_hash", observed_hash
    )
    assert (await approve(case)).data.status == "succeeded"
    state = await observe(case)
    assert_success(case, state)
    business_transaction = case.calls[0][1]
    business_user = case.created_user_ids[0]
    assert (
        business_transaction,
        business_user,
        state["data_scope_hash"],
    ) in scope_observations
    assert state["data_scope_hash"] is not None
