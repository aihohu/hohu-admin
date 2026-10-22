"""Bounded PA setup and fact-only readback after preview handoff uncertainty."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
    _commit_with_budget,
    _discard_uncertain_session,
    _owned_session,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.preview_completion_service import lock_handoff


@asynccontextmanager
async def handoff_transaction(db):
    try:
        await db.begin()
        yield
        await db.flush()
        await _commit_with_budget(db)
    except (Exception, asyncio.CancelledError):
        await _discard_uncertain_session(db)
        raise


async def read_handoff(*, context, deps, log_id, payload):
    try:
        async with asyncio.timeout(8), _owned_session() as db:
            await db.begin()
            action = await lock_handoff(
                db, context=context, deps=deps, log_id=log_id, read_existing=True
            )
            prepared_action_service.validate_pending_binding(action, payload)
            proposal_expiry = context.proposal.expires_at
            if proposal_expiry.tzinfo is None:
                proposal_expiry = proposal_expiry.replace(tzinfo=UTC)
            payload_expiry = datetime.fromisoformat(
                payload.expires_at.replace("Z", "+00:00")
            )
            if action.expires_at != min(proposal_expiry, payload_expiry):
                raise PreparedExecutionOutcomeUnknown()
            return action
    except (Exception, asyncio.CancelledError) as exc:
        raise PreparedExecutionOutcomeUnknown() from exc
