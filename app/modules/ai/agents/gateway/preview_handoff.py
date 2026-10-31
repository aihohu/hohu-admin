"""Bounded PA setup and fact-only readback after preview handoff uncertainty."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC

from app.db.authorization_http import get_authorization_runtime
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
    _commit_with_budget,
    _discard_uncertain_session,
)
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.auth.service import access_identity


def handoff_request(*, context, deps, log_id, payload):
    from app.modules.ai.agents.gateway.executor import (  # noqa: PLC0415
        _direct_pending_values,
    )

    identity = access_identity(deps.user)
    if (
        identity.tenant != deps.tenant
        or context.prepare_tool_name != "user.import_preview"
    ):
        raise PreparedExecutionOutcomeUnknown()
    proposal = context.proposal
    values = _direct_pending_values(
        deps,
        ToolRegistry.get().find("user.import_execute"),
        context.execute_tool_call_id,
        proposal.frozen_args,
        None,
        payload,
        context.confirmation_id,
    )
    expiry = proposal.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    values.update(
        prepare_tool_call_id=context.prepare_tool_call_id,
        prepare_tool_name=context.prepare_tool_name,
        snapshot=proposal.snapshot,
        snapshot_hash=proposal.snapshot_hash,
        subject_ref=proposal.subject_ref,
        presentation=proposal.presentation,
        interaction_flow="prepared",
        requested_outcome="execute_if_approved",
        data_scope_hash=deps.data_scope_hash,
        expires_at=min(expiry, values["expires_at"]),
    )
    return {
        "tenant": identity.tenant,
        "actor_user_version": identity.user_version,
        "log_id": log_id,
        "original_args_hash": compute_args_hash(proposal.frozen_args),
        "scope_hash": deps.data_scope_hash,
        "values": values,
        "preview_expiry": expiry,
    }


async def prepare_handoff(*, context, deps, log_id, payload):
    return await get_authorization_runtime().pending.handoff(
        **handoff_request(context=context, deps=deps, log_id=log_id, payload=payload)
    )


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
        action = await get_authorization_runtime().pending.read_handoff(
            **handoff_request(
                context=context, deps=deps, log_id=log_id, payload=payload
            )
        )
        prepared_action_service.validate_pending_binding(action, payload)
        return action
    except (Exception, asyncio.CancelledError) as exc:
        raise PreparedExecutionOutcomeUnknown() from exc
