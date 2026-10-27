"""Classify only the caller's complete, locked completion snapshot."""

from app.core.exceptions import BusinessRuleException
from app.modules.ai.service.completion_read_state_service import (
    UNKNOWN_COMPLETION,
    EffectiveCompletion,
    completion_read_state_service,
)
from app.modules.ai.service.preflight_completion_service import is_verified_preflight
from app.modules.ai.service.preview_completion_service import verify_historical_preview


async def project_completions(db, *, tenant, actions, logs, conversations, sources):
    by_call = {row.execute_tool_call_id: row for row in actions}
    by_log = {row.tool_call_id: row for row in logs}
    states = {}
    for call in set(by_call) | set(by_log):
        action, log = by_call.get(call), by_log.get(call)
        if not completion_read_state_service._protected(action, log):
            continue
        reference = action or log
        conversation = conversations.get(reference.conversation_id)
        source = sources.get(reference.source_user_message_id)
        if action is None and log is not None:
            try:
                if log.tool_name == "user.import_preview":
                    await verify_historical_preview(
                        db,
                        log,
                        conversation,
                        source,
                        tenant_id=tenant.tenant_id,
                        user_id=log.user_id,
                    )
                elif not await is_verified_preflight(
                    db,
                    log,
                    conversation,
                    source,
                    tenant_id=tenant.tenant_id,
                    user_id=log.user_id,
                ):
                    states[call] = UNKNOWN_COMPLETION
                    continue
                states[call] = EffectiveCompletion(
                    log.status,
                    log.error_code,
                    log.finished_at,
                    log.duration_ms,
                    verified=True,
                )
            except (BusinessRuleException, TypeError, ValueError):
                states[call] = UNKNOWN_COMPLETION
        else:
            states[call] = completion_read_state_service._project(
                action, log, conversation, source, tenant=tenant
            )
    return states
