"""The production claimed-execution seam invokes only planned fixed adapters."""

from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update

from app.core.exceptions import BusinessException
from app.core.security import create_access_token
from app.db.request_session import RequestSession
from app.modules.ai.agents.gateway import (
    atomic_execution,
    authorization_execution,
    executor,
)
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.auth.service import get_current_user
from app.modules.system.models.dept import Dept
from tests.modules.ai.test_authorization_conversation_http import (
    chat_model_case as chat_model_case,
)
from tests.modules.ai.test_authorization_conversation_http import (
    conversation_http as conversation_http,
)
from tests.modules.ai.test_authorization_conversation_http import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_conversation_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.ai.test_authorization_conversation_http import (
    recovery_case as recovery_case,
)
from tests.modules.ai.test_authorization_conversation_http import (
    system_sessions as system_sessions,
)


@pytest.mark.parametrize("unknown", [False, True])
async def test_real_claim_uses_planned_owner_no_handler_replay(
    conversation_http, monkeypatch, unknown
):
    _, plain, case = conversation_http
    engine = plain.kw["bind"]
    locks, commits = [], []

    def sql(_c, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    def commit(connection):
        commits.append(connection)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Production claim cannot use the legacy arbitrary handler")

    monkeypatch.setattr(atomic_execution, "execute_claim", forbidden)
    if unknown:
        original = authorization_execution.commit_authorization_session

        async def lost(db):
            await original(db)
            raise BusinessException(
                503, "Unknown ACK", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
            )

        monkeypatch.setattr(
            authorization_execution, "commit_authorization_session", lost
        )
    async with RequestSession(bind=engine, expire_on_commit=False) as db:
        user = await get_current_user(
            create_access_token(
                subject="101", tenant_id=0, tenant_version=1, user_version=1
            ),
            db,
        )
        deps = SimpleNamespace(user=user, tenant=case.tenant, db=db)
        db.expunge_all()
        await db.rollback()
        event.listen(engine.sync_engine, "before_cursor_execute", sql)
        event.listen(engine.sync_engine, "commit", commit)
        try:
            if unknown:
                with pytest.raises(atomic_execution.PreparedExecutionOutcomeUnknown):
                    await executor.execute_approved_prepared_action(case.claim, deps)
            else:
                result = await executor.execute_approved_prepared_action(
                    case.claim, deps
                )
                assert result.disposition == "succeeded"
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", sql)
            event.remove(engine.sync_engine, "commit", commit)
    assert len(commits) == 1
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    async with plain() as db:
        assert (
            len(
                (
                    await db.scalars(
                        select(Dept).where(Dept.dept_name == "Gateway team")
                    )
                ).all()
            )
            == 1
        )
        assert (
            await db.scalar(
                select(AiPreparedAction.status).where(
                    AiPreparedAction.action_id == case.claim.action_id
                )
            )
            == "succeeded"
        )
        assert (
            await db.scalar(
                select(AiOperationLog.status).where(
                    AiOperationLog.log_id == case.claim.log_id
                )
            )
            == "success"
        )


@pytest.mark.parametrize(
    "modes",
    [("hitl", "execute_after_approval"), ("required", "inline"), ("auto", "inline")],
)
async def test_mixed_or_unknown_approval_metadata_cannot_execute(
    conversation_http, modes
):
    _, plain, case = conversation_http
    async with plain() as setup:
        await setup.execute(
            update(AiPreparedAction)
            .where(AiPreparedAction.action_id == case.claim.action_id)
            .values(approval_mode=modes[0], dispatch_mode=modes[1])
        )
        await setup.commit()
    async with RequestSession(bind=plain.kw["bind"], expire_on_commit=False) as db:
        user = await get_current_user(
            create_access_token(
                subject="101", tenant_id=0, tenant_version=1, user_version=1
            ),
            db,
        )
        deps = SimpleNamespace(user=user, tenant=case.tenant, db=db)
        db.expunge_all()
        await db.rollback()
        result = await executor.execute_approved_prepared_action(case.claim, deps)
        assert result.disposition == "failed"
    async with plain() as db:
        assert (
            await db.scalar(
                select(Dept.dept_id).where(Dept.dept_name == "Gateway team")
            )
            is None
        )


async def test_claim_cannot_run_with_request_transaction_open(conversation_http):
    _, plain, case = conversation_http
    async with RequestSession(bind=plain.kw["bind"], expire_on_commit=False) as db:
        user = await get_current_user(
            create_access_token(
                subject="101", tenant_id=0, tenant_version=1, user_version=1
            ),
            db,
        )
        with pytest.raises(BusinessException) as error:
            await executor.execute_approved_prepared_action(
                case.claim, SimpleNamespace(user=user, tenant=case.tenant, db=db)
            )
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
