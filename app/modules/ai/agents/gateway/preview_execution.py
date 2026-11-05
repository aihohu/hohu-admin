"""Fixed preview publication; uncertainty only reads committed success facts."""

import asyncio
import json

from app.db.authorization_http import get_authorization_runtime
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.service.authorization_preview_candidate import GatewayPreviewBinding
from app.modules.auth.service import access_identity


def _request(deps, *, log_id, tool_call_id, args_hash, args=None):
    identity = access_identity(deps.user)
    if identity.tenant != deps.tenant:
        raise PreparedExecutionOutcomeUnknown()
    return {
        "tenant": identity.tenant,
        "actor_user_version": identity.user_version,
        "conversation_id": deps.conversation_id,
        "source_id": deps.source_user_message_id,
        "trace_id": deps.trace_id,
        "gateway": GatewayPreviewBinding(
            log_id,
            tool_call_id,
            args_hash,
            json.dumps(args, ensure_ascii=False, allow_nan=False)
            if args is not None
            else None,
            deps.data_scope_hash,
            deps.resolved_model_id,
            deps.resolved_provider_id,
        ),
    }


async def read_preview_result(*, log_id, tool_call_id, args_hash, deps):
    """No parser, file IO, proposal or confirmation can run on this path."""
    try:
        return await get_authorization_runtime().preview.receipt(
            **_request(
                deps, log_id=log_id, tool_call_id=tool_call_id, args_hash=args_hash
            )
        )
    except (Exception, asyncio.CancelledError) as exc:
        raise PreparedExecutionOutcomeUnknown() from exc


async def execute_preview(registered, args, deps, *, log_id, tool_call_id, args_hash):
    if registered.meta.name != "user.import_preview":
        raise PreparedExecutionOutcomeUnknown()
    try:
        return await get_authorization_runtime().preview.gateway_preview(
            **_request(
                deps,
                log_id=log_id,
                tool_call_id=tool_call_id,
                args_hash=args_hash,
                args=args,
            )
        )
    except (Exception, asyncio.CancelledError) as exc:
        # Only a known committed preview may return a minimal receipt.
        try:
            return await read_preview_result(
                log_id=log_id, tool_call_id=tool_call_id, args_hash=args_hash, deps=deps
            )
        except PreparedExecutionOutcomeUnknown as unknown:
            raise unknown from exc
