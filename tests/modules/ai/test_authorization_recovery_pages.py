"""Cross-tenant recovery discovers bounded pages; each item owns fresh barriers."""

import pytest
from sqlalchemy import insert

from app.core.exceptions import BusinessException
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from tests.modules.ai.test_authorization_recovery_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.ai.test_authorization_recovery_candidate import (
    recovery_case as recovery_case,
)
from tests.modules.ai.test_authorization_recovery_candidate import (
    system_sessions as system_sessions,
)


async def test_recovery_page_has_stable_cursor_and_no_result_body(recovery_case):
    factory, _, case = recovery_case
    owner = AIAuthorizationOwner(factory)
    page = await owner.recover_page(limit=1)
    assert len(page.items) == 1
    assert page.items[0].disposition == "unknown"
    assert page.next_cursor == (0, case.action.action_id)
    assert not hasattr(page.items[0], "action")
    assert not (await owner.recover_page(after=page.next_cursor, limit=1)).items


@pytest.mark.parametrize("limit", [0, 257, True])
async def test_recovery_page_rejects_unbounded_sizes(recovery_case, limit):
    factory, _, _ = recovery_case
    with pytest.raises(BusinessException):
        await AIAuthorizationOwner(factory).recover_page(limit=limit)


async def test_recovery_page_over_32_tenants_uses_individual_transactions(
    recovery_case,
):
    factory, plain, case = recovery_case
    action_values = {
        column.name: getattr(case.action, column.name)
        for column in AiPreparedAction.__table__.columns
    }
    log_values = {
        column.name: getattr(case.log, column.name)
        for column in AiOperationLog.__table__.columns
    }
    async with plain() as db:
        for tid in range(2, 35):
            uid, cid, mid, aid, lid = (
                base + tid for base in (1000, 2000, 3000, 4000, 5000)
            )
            await db.execute(
                insert(Tenant).values(
                    tenant_id=tid,
                    tenant_code=f"recovery-{tid}",
                    tenant_name="recovery",
                    status="1",
                    lifecycle_state="active",
                    bootstrap_version=1,
                    row_version=1,
                    bootstrap_key_hash=format(tid, "064x"),
                    bootstrap_fingerprint="b" * 64,
                )
            )
            await db.execute(
                insert(User).values(
                    tenant_id=tid,
                    user_id=uid,
                    user_name="recovery",
                    hashed_password="not-a-credential",
                    status="1",
                )
            )
            await db.execute(
                insert(AiConversation).values(
                    tenant_id=tid, conversation_id=cid, user_id=uid
                )
            )
            await db.execute(
                insert(AiMessage).values(
                    tenant_id=tid,
                    message_id=mid,
                    conversation_id=cid,
                    role="user",
                    trace_id=f"trace-{tid}",
                )
            )
            binding = {
                "tenant_id": tid,
                "user_id": uid,
                "conversation_id": cid,
                "source_user_message_id": mid,
                "trace_id": f"trace-{tid}",
                "confirmation_id": f"confirmation-{tid}",
                "approved_by": uid,
            }
            await db.execute(
                insert(AiPreparedAction).values(
                    action_values
                    | binding
                    | {"action_id": aid, "execute_tool_call_id": f"call-{tid}"}
                )
            )
            await db.execute(
                insert(AiOperationLog).values(
                    log_values
                    | binding
                    | {"log_id": lid, "tool_call_id": f"call-{tid}"}
                )
            )
        await db.commit()
    owner = AIAuthorizationOwner(factory)
    first = await owner.recover_page(limit=20)
    second = await owner.recover_page(after=first.next_cursor, limit=20)
    items = first.items + second.items
    assert len(items) == 34
    assert len({item.tenant_id for item in items}) == 34
    assert all(item.disposition == "unknown" for item in items)
    assert not (await owner.recover_page(after=second.next_cursor, limit=20)).items
