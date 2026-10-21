"""Immutable read projections from one committed PA/log/context snapshot.

Unlike recovery, this reader acquires no execution locks and never infers a
rollback or fences an owner. One joined statement supplies every proof field.
"""

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import and_, or_, select, text

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    FILE_TOOLS,
    atomic_completion_service,
    has_protocol_marker,
)

logger = logging.getLogger(__name__)
UNKNOWN_CODE = "AI_EXECUTION_OUTCOME_UNKNOWN"
_WRITE_TOOLS = DB_TOOLS | FILE_TOOLS
_READ_TIMEOUT_SEC = 8


@dataclass(frozen=True, slots=True)
class EffectiveCompletion:
    status: str
    error_code: str | None
    finished_at: datetime | None
    duration_ms: int | None
    verified: bool = False

    @property
    def unknown(self) -> bool:
        return self.error_code == UNKNOWN_CODE


UNKNOWN_COMPLETION = EffectiveCompletion("running", UNKNOWN_CODE, None, None)


def effective_log_state(
    log: AiOperationLog, states: Mapping[str, EffectiveCompletion]
) -> EffectiveCompletion:
    state = states.get(log.tool_call_id)
    if state is not None:
        return state
    if CompletionReadStateService._protected(None, log):
        # The row may have disappeared since the caller loaded it. Absence is
        # never proof that its stale terminal status is trustworthy.
        return UNKNOWN_COMPLETION
    return EffectiveCompletion(
        log.status, log.error_code, log.finished_at, log.duration_ms
    )


def effective_call_state(
    call: dict, states: Mapping[str, EffectiveCompletion]
) -> EffectiveCompletion | None:
    state = states.get(call.get("tool_call_id"))
    if state is None and call.get("tool") in _WRITE_TOOLS:
        state = states.get("")
    return state


def project_verified_call(
    call: dict, states: Mapping[str, EffectiveCompletion]
) -> dict:
    state = effective_call_state(call, states)
    if state is None or not state.verified:
        return call
    output = dict(call)
    expected_ok = state.status == "success"
    if call.get("ok") is not expected_ok or call.get("error_code") != state.error_code:
        # A stale card is not a source of business data for the proven outcome.
        for field in ("result", "ui", "error_msg"):
            output.pop(field, None)
    output.update(
        ok=expected_ok, error_code=state.error_code, duration_ms=state.duration_ms
    )
    return output


class CompletionReadStateService:
    async def read(
        self,
        *,
        tenant: TenantContext,
        tool_call_ids: Iterable[str] | None = None,
        conversation_id: int | None = None,
        user_id: int | None = None,
    ) -> dict[str, EffectiveCompletion]:
        if tool_call_ids is None and (conversation_id is None or user_id is None):
            raise ValueError("completion reads require IDs or an owned conversation")
        identifiers = tuple(sorted({value for value in tool_call_ids or () if value}))
        if tool_call_ids is not None and not identifiers:
            return {}
        log_scope = [AiOperationLog.tenant_id == tenant.tenant_id]
        action_scope = [AiPreparedAction.tenant_id == tenant.tenant_id]
        if tool_call_ids is not None:
            log_scope.append(AiOperationLog.tool_call_id.in_(identifiers))
            action_scope.append(AiPreparedAction.execute_tool_call_id.in_(identifiers))
        if conversation_id is not None:
            log_scope.append(AiOperationLog.conversation_id == conversation_id)
            action_scope.append(AiPreparedAction.conversation_id == conversation_id)
        if user_id is not None:
            log_scope.append(AiOperationLog.user_id == user_id)
            action_scope.append(AiPreparedAction.user_id == user_id)
        statement = (
            select(AiPreparedAction, AiOperationLog, AiConversation, AiMessage)
            .select_from(AiOperationLog)
            .join(
                AiPreparedAction,
                and_(
                    AiPreparedAction.tenant_id == AiOperationLog.tenant_id,
                    AiPreparedAction.execute_tool_call_id
                    == AiOperationLog.tool_call_id,
                ),
                full=True,
            )
            .outerjoin(
                AiConversation,
                and_(
                    AiConversation.tenant_id == AiPreparedAction.tenant_id,
                    AiConversation.conversation_id == AiPreparedAction.conversation_id,
                ),
            )
            .outerjoin(
                AiMessage,
                and_(
                    AiMessage.tenant_id == AiPreparedAction.tenant_id,
                    AiMessage.message_id == AiPreparedAction.source_user_message_id,
                ),
            )
            .where(or_(and_(*log_scope), and_(*action_scope)))
            .execution_options(populate_existing=True)
        )
        from app.modules.ai.agents.gateway.atomic_execution import (  # noqa: PLC0415
            _discard_uncertain_session,
            _owned_session,
        )

        try:
            async with asyncio.timeout(_READ_TIMEOUT_SEC), _owned_session() as db:
                try:
                    await db.begin()
                    await db.execute(text("SET TRANSACTION READ ONLY"))
                    rows = (await db.execute(statement)).all()
                    projected = {}
                    for action, log, conversation, source in rows:
                        if not self._protected(action, log):
                            continue
                        call_id = (
                            action.execute_tool_call_id if action else log.tool_call_id
                        )
                        projected[call_id] = self._project(
                            action, log, conversation, source, tenant=tenant
                        )
                    return projected
                except (Exception, asyncio.CancelledError):
                    await _discard_uncertain_session(db)
                    raise
        except (Exception, asyncio.CancelledError):
            logger.warning("completion read snapshot unavailable", exc_info=True)
            # Do not expose an unverified raw terminal after a read failure.
            # A scope-only reader uses the values to block admission; the empty
            # internal key never becomes a public tool-call identifier.
            return {"": UNKNOWN_COMPLETION}

    @staticmethod
    def _protected(action, log) -> bool:
        if action is not None and (
            has_protocol_marker(action.execution_owner)
            or (
                action.status in {"succeeded", "rejected", "expired"}
                and action.execute_tool_name in _WRITE_TOOLS
            )
        ):
            return True
        return bool(
            log is not None
            and log.tool_name in _WRITE_TOOLS
            and getattr(log, "readonly_snapshot", False) is not True
            and log.status in {"running", "success", "failed", "rejected", "expired"}
        )

    @staticmethod
    def _project(action, log, conversation, source, *, tenant) -> EffectiveCompletion:
        if action is None or log is None:
            return UNKNOWN_COMPLETION
        if action.status == "failed" and (
            action.execute_tool_name not in DB_TOOLS
            or not (action.execution_owner or "").startswith("v0162-db:")
        ):
            return UNKNOWN_COMPLETION
        try:
            validator = (
                atomic_completion_service.validate_locked_unexecuted_terminal
                if action.status in {"rejected", "expired"}
                else atomic_completion_service.validate_locked_terminal
            )
            validator(
                action,
                log,
                tenant=replace(tenant, actor_user_id=action.user_id),
                conversation=conversation,
                source=source,
            )
        except (BusinessRuleException, TypeError, ValueError):
            return UNKNOWN_COMPLETION
        return EffectiveCompletion(
            log.status, log.error_code, log.finished_at, log.duration_ms, verified=True
        )


completion_read_state_service = CompletionReadStateService()
