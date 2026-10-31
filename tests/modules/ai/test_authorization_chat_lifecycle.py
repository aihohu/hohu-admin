"""A complete protected chat turn and its current-authority projections."""

import pytest
from sqlalchemy import insert, select, update

from app.core.exceptions import BusinessException
from app.db.authorization_chat_owner import ChatOwner
from app.db.authorization_lifecycle_owner import LifecycleOwner
from app.db.authorization_trace_owner import TraceOwner
from app.db.base import role_menus
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.schemas.operation_log import TraceListQuery
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from tests.modules.ai.test_authorization_gateway_execution import execute
from tests.modules.ai.test_authorization_gateway_execution import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_recovery_candidate import (
    recovery_case as recovery_case,
)
from tests.modules.system.test_authorization_system_candidate import epoch
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)


@pytest.fixture
async def chat_model_case(execution_case):
    factory, plain, case = execution_case
    async with plain() as db:
        await db.execute(
            insert(AiProvider).values(
                provider_id=7101,
                provider_code="openai",
                name="Test",
                api_key="encrypted-fixture",
                base_url="https://api.openai.com",
                is_enabled=True,
            )
        )
        await db.execute(
            insert(AiModel).values(
                model_id=7102,
                provider_id=7101,
                name="test",
                capabilities=["text"],
                is_enabled=True,
            )
        )
        await db.execute(
            insert(TenantAiModelPolicy).values(
                tenant_id=0, model_id=7102, enabled=True, is_default=True
            )
        )
        await db.execute(update(AiConversation).values(model_name="7102"))
        await db.commit()
    return factory, plain, case


async def test_conversation_model_preparation_is_outside_barriers(
    chat_model_case, monkeypatch
):

    factory, plain, case = chat_model_case
    calls = []

    async def allowed(*_args, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(provider_egress, "is_model_allowed", allowed)
    identifier = await ChatOwner(factory).conversation(
        tenant=case.tenant,
        actor_user_version=1,
        data={"title": "New", "model_name": "7102"},
    )
    assert calls
    async with plain() as db:
        row = await db.get(AiConversation, identifier)
        assert row.title == "New" and row.model_name == "7102" and await epoch(db) == 1


async def test_source_finalize_and_projection_use_same_context(execution_case):

    factory, plain, case = execution_case
    assert (await execute(factory, case)).disposition == "succeeded"
    owner = ChatOwner(factory)
    source = await owner.source(
        conversation_id=case.claim.conversation_id,
        content="new turn",
        tenant=case.tenant,
        actor_user_version=1,
    )
    message_id = await owner.finalize(
        conversation_id=case.claim.conversation_id,
        source_id=source.message_id,
        trace_id=source.trace_id,
        content="answer",
        tool_calls=[],
        agent_code="dept_mgmt",
        tenant=case.tenant,
        actor_user_version=1,
    )
    assert (
        await owner.finalize(
            conversation_id=case.claim.conversation_id,
            source_id=source.message_id,
            trace_id=source.trace_id,
            content="duplicate",
            tool_calls=[],
            agent_code="dept_mgmt",
            tenant=case.tenant,
            actor_user_version=1,
        )
        == message_id
    )
    messages = await owner.messages(
        conversation_id=case.claim.conversation_id,
        tenant=case.tenant,
        actor_user_version=1,
    )
    assert messages[-1].content == "answer"
    async with plain() as db:
        assert await epoch(db) == 2
        assert (
            len(
                list(
                    await db.scalars(
                        select(AiMessage).where(AiMessage.trace_id == source.trace_id)
                    )
                )
            )
            == 2
        )


async def test_message_reader_omits_inactive_and_redacts_without_writing(
    execution_case,
):
    factory, plain, case = execution_case
    secret = "sk-" + "A" * 32
    async with plain() as db:
        await db.execute(
            update(AiMessage)
            .where(AiMessage.message_id == case.claim.source_user_message_id)
            .values(content=secret)
        )
        await db.execute(
            insert(AiMessage).values(
                tenant_id=0,
                message_id=19001,
                conversation_id=case.claim.conversation_id,
                role="user",
                content="inactive body",
                is_active=False,
            )
        )
        await db.commit()
    messages = await ChatOwner(factory).messages(
        conversation_id=case.claim.conversation_id,
        tenant=case.tenant,
        actor_user_version=1,
    )
    assert len(messages) == 1 and messages[0].content == "[REDACTED:OPENAI_API_KEY]"
    async with plain() as db:
        assert (
            await db.get(AiMessage, case.claim.source_user_message_id)
        ).content == secret


async def test_context_delete_retains_unknown_and_cancels_only_pending(execution_case):
    factory, plain, case = execution_case
    owner = ChatOwner(factory)
    with pytest.raises(BusinessException):
        await owner.invalidate(
            conversation_id=case.claim.conversation_id,
            tenant=case.tenant,
            actor_user_version=1,
        )
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                status="pending_confirmation",
                row_version=1,
                approved_by=None,
                approved_at=None,
                execution_owner=None,
                execution_lease_expires_at=None,
            )
        )
        await db.execute(
            update(AiOperationLog).values(
                status="pending_confirmation", approved_by=None, started_at=None
            )
        )
        await db.commit()
    await owner.invalidate(
        conversation_id=case.claim.conversation_id,
        tenant=case.tenant,
        actor_user_version=1,
    )
    async with plain() as db:
        assert (
            await db.get(AiConversation, case.claim.conversation_id)
        ).deleted_at is not None
        assert await db.scalar(select(AiPreparedAction.status)) == "expired"
        assert await epoch(db) == 1


async def test_finalizer_does_not_invent_completion(execution_case):

    factory, plain, case = execution_case
    owner = ChatOwner(factory)
    await owner.finalize(
        conversation_id=case.claim.conversation_id,
        source_id=case.claim.source_user_message_id,
        trace_id=case.claim.trace_id,
        content="created",
        tool_calls=[
            {
                "tool": "dept.create",
                "tool_call_id": case.claim.execute_tool_call_id,
                "ok": True,
                "result": {"secret": "forged"},
            }
        ],
        agent_code="dept_mgmt",
        tenant=case.tenant,
        actor_user_version=1,
    )
    messages = await owner.messages(
        conversation_id=case.claim.conversation_id,
        tenant=case.tenant,
        actor_user_version=1,
    )
    assert "forged" not in str(messages)
    assert "AI_EXECUTION_OUTCOME_UNKNOWN" in messages[-1].content
    async with plain() as db:
        await db.execute(update(User).where(User.user_id == 101).values(auth_version=2))
        await db.commit()
    with pytest.raises(BusinessException):
        await owner.messages(
            conversation_id=case.claim.conversation_id,
            tenant=case.tenant,
            actor_user_version=1,
        )


async def test_trace_reader_rechecks_permission_and_reports_unknown(execution_case):
    factory, plain, case = execution_case
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9080,
                menu_name="trace",
                menu_type="F",
                status="1",
                permission="ai:trace:view",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9080)
        )
        await db.commit()
    owner = TraceOwner(factory)
    page = await owner.list(
        tenant=case.tenant, actor_user_version=1, query=TraceListQuery()
    )
    assert page.total == 1 and page.records[0].statuses == ["running"]
    detail = await owner.detail(
        tenant=case.tenant, actor_user_version=1, trace_id=case.claim.trace_id
    )
    assert (
        detail.operations[0].status == "running"
        and detail.operations[0].error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
        and not hasattr(detail.operations[0], "args")
    )


async def test_pending_creation_and_dry_run_have_no_business_writes(chat_model_case):

    factory, plain, case = chat_model_case
    async with plain() as db:
        prior = await db.scalar(select(AiPreparedAction))
        args, snapshot = dict(prior.frozen_args), dict(prior.snapshot["business"])
    owner = LifecycleOwner(factory)
    kwargs = {
        "tool": "dept.create",
        "args": args,
        "snapshot": snapshot,
        "conversation_id": case.claim.conversation_id,
        "source_id": case.claim.source_user_message_id,
        "trace_id": case.claim.trace_id,
        "tenant": case.tenant,
        "actor_user_version": 1,
    }
    assert await owner.prepare(**kwargs, dry_run=True) is None
    confirmation = await owner.prepare(**kwargs)
    async with plain() as db:
        rows = list(await db.scalars(select(AiPreparedAction)))
        assert len(rows) == 2 and await epoch(db) == 1
        pending = next(row for row in rows if row.confirmation_id == confirmation)
        assert (
            pending.status == "pending_confirmation" and pending.execution_owner is None
        )
    assert (
        await owner.confirm(confirmation, tenant=case.tenant, actor_user_version=1)
    ).disposition == "succeeded"


@pytest.mark.parametrize("revoke", [False, True])
async def test_plain_finalizer_keeps_and_rechecks_source_dependencies(
    execution_case, revoke
):
    factory, plain, case = execution_case
    assert (await execute(factory, case)).disposition == "succeeded"
    owner = ChatOwner(factory)
    prior = await owner.finalize(
        conversation_id=case.claim.conversation_id,
        source_id=case.claim.source_user_message_id,
        trace_id=case.claim.trace_id,
        content="authorized department detail",
        tool_calls=[],
        agent_code="dept_mgmt",
        tenant=case.tenant,
        actor_user_version=1,
    )
    source = await owner.source(
        conversation_id=case.claim.conversation_id,
        content="summarize prior answer",
        tenant=case.tenant,
        actor_user_version=1,
    )
    async with plain() as db:
        assert (
            str(prior)
            in (
                await db.get(AiMessage, source.message_id)
            ).projection_dependency_message_ids
        )
        if revoke:
            await db.execute(
                update(RoleAiAgent)
                .where(RoleAiAgent.tenant_id == 0)
                .values(enabled=False)
            )
            await db.commit()
    kwargs = {
        "conversation_id": case.claim.conversation_id,
        "source_id": source.message_id,
        "trace_id": source.trace_id,
        "content": "derived detail",
        "tool_calls": [],
        "agent_code": None,
        "tenant": case.tenant,
        "actor_user_version": 1,
    }
    if revoke:
        with pytest.raises(BusinessException):
            await owner.finalize(**kwargs)
    else:
        identifier = await owner.finalize(**kwargs)
        async with plain() as db:
            row = await db.get(AiMessage, identifier)
            assert row.projection_dependency_message_ids == [str(prior)]
            await db.execute(
                update(RoleAiAgent)
                .where(RoleAiAgent.tenant_id == 0)
                .values(enabled=False)
            )
            await db.commit()
        messages = await owner.messages(
            conversation_id=case.claim.conversation_id,
            tenant=case.tenant,
            actor_user_version=1,
        )
        projected = next(row for row in messages if row.message_id == identifier)
        assert not hasattr(projected, "content")
        assert all(
            getattr(row, "content", None) != "derived detail" for row in messages
        )
        with pytest.raises(BusinessException):
            await owner.finalize(**kwargs)
