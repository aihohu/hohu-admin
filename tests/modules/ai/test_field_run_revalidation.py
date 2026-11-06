"""A source freezes authority; revocation cannot rewrite or replay its facts."""

import pytest
from sqlalchemy import select, update

from app.core.exceptions import AuthorizationException
from app.db.authorization_chat_owner import ChatOwner
from app.db.authorization_lifecycle_owner import LifecycleOwner
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.system.models.dept import Dept
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from tests.modules.ai.test_authorization_chat_lifecycle import (
    chat_model_case as chat_model_case,
)
from tests.modules.ai.test_authorization_chat_lifecycle import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_chat_lifecycle import (
    plan_sessions as plan_sessions,
)
from tests.modules.ai.test_authorization_chat_lifecycle import (
    recovery_case as recovery_case,
)
from tests.modules.ai.test_authorization_chat_lifecycle import (
    system_sessions as system_sessions,
)
from tests.modules.ai.test_authorization_gateway_execution import execute


async def test_claimed_work_epoch_change_never_dispatches(execution_case, caplog):
    factory, plain, case = execution_case
    frozen = await ChatOwner(factory).model_authority(
        tenant=case.tenant, actor_user_version=1
    )
    async with plain() as db:
        # Construct the new-protocol fixture; production never backfills history.
        await db.execute(
            update(AiPreparedAction)
            .where(AiPreparedAction.action_id == case.claim.action_id)
            .values(field_authority=frozen)
        )
        await db.execute(
            update(UserFieldPolicyState)
            .where(UserFieldPolicyState.tenant_id == case.tenant.tenant_id)
            .values(epoch=UserFieldPolicyState.epoch + 1)
        )
        await db.commit()
    assert (await execute(factory, case)).disposition == "failed"
    async with plain() as db:
        assert await db.scalar(select(Dept)) is None
        action = await db.get(AiPreparedAction, case.claim.action_id)
        assert action.error_code == "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
        assert action.field_authority == frozen
    assert "FIELD_AUTHORIZATION_CHANGED" in caplog.text


async def test_pending_confirmation_epoch_change_denies_before_claim(chat_model_case):
    factory, plain, case = chat_model_case
    frozen = await ChatOwner(factory).model_authority(
        tenant=case.tenant, actor_user_version=1
    )
    async with plain() as db:
        prior = await db.scalar(select(AiPreparedAction))
        args, snapshot = dict(prior.frozen_args), dict(prior.snapshot["business"])
        await db.execute(
            update(AiMessage)
            .where(AiMessage.message_id == case.claim.source_user_message_id)
            .values(field_authority=frozen)
        )
        await db.commit()
    owner = LifecycleOwner(factory)
    confirmation = await owner.prepare(
        tool="dept.create",
        args=args,
        snapshot=snapshot,
        conversation_id=case.claim.conversation_id,
        source_id=case.claim.source_user_message_id,
        trace_id=case.claim.trace_id,
        tenant=case.tenant,
        actor_user_version=1,
    )
    async with plain() as db:
        await db.execute(
            update(UserFieldPolicyState)
            .where(UserFieldPolicyState.tenant_id == case.tenant.tenant_id)
            .values(epoch=UserFieldPolicyState.epoch + 1)
        )
        await db.commit()
    admitted = await owner.admit(
        confirmation, tenant=case.tenant, actor_user_version=1, decision="approve"
    )
    assert admitted.error.error_code == "FIELD_AUTHORIZATION_CHANGED"
    async with plain() as db:
        assert await db.scalar(select(Dept)) is None
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.confirmation_id == confirmation
            )
        )
        assert action.execution_owner is None
        assert action.field_authority == frozen


async def test_epoch_change_stops_run_and_hides_prior_answer_without_mutation(
    execution_case,
):
    factory, plain, case = execution_case
    assert (await execute(factory, case)).disposition == "succeeded"
    owner = ChatOwner(factory)
    source = await owner.source(
        conversation_id=case.claim.conversation_id,
        content="new turn",
        tenant=case.tenant,
        actor_user_version=1,
    )
    request = {
        "conversation_id": case.claim.conversation_id,
        "source_id": source.message_id,
        "trace_id": source.trace_id,
        "tenant": case.tenant,
        "actor_user_version": 1,
    }
    await owner.check_run(**request)
    message_id = await owner.finalize(
        **request, content="sensitive answer", tool_calls=[], agent_code="dept_mgmt"
    )
    async with plain() as db:
        frozen = (await db.get(AiMessage, source.message_id)).field_authority
        await db.execute(
            update(UserFieldPolicyState)
            .where(UserFieldPolicyState.tenant_id == case.tenant.tenant_id)
            .values(epoch=UserFieldPolicyState.epoch + 1)
        )
        await db.commit()
    with pytest.raises(AuthorizationException) as caught:
        await owner.check_run(**request)
    assert caught.value.error_code == "FIELD_AUTHORIZATION_CHANGED"
    projected = await owner.messages(
        conversation_id=case.claim.conversation_id,
        tenant=case.tenant,
        actor_user_version=1,
    )
    assert all(
        getattr(item, "content", None) != "sensitive answer" for item in projected
    )
    async with plain() as db:
        assert (await db.get(AiMessage, source.message_id)).field_authority == frozen
        assert (
            await db.scalar(
                select(AiMessage.content).where(AiMessage.message_id == message_id)
            )
            == "sensitive answer"
        )
