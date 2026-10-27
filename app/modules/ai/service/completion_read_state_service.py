"""Immutable read projections from one committed PA/log/context snapshot.

Unlike recovery, this reader acquires no execution locks and never infers a
rollback or fences an owner. PA proofs use one joined statement; preview proofs
also check their batch in the same REPEATABLE READ, READ ONLY snapshot.
"""

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import and_, func, or_, select, text

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
from app.modules.ai.service.reconciliation_annotation import NOTICE, has_annotation

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
        return self.error_code in {UNKNOWN_CODE, "AI_EXECUTION_RECONCILIATION_REQUIRED"}


UNKNOWN_COMPLETION = EffectiveCompletion("running", UNKNOWN_CODE, None, None)
RECONCILIATION_COMPLETION = EffectiveCompletion(
    "running", "AI_EXECUTION_RECONCILIATION_REQUIRED", None, None
)


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


def project_verified_content(content, calls, states):
    """Hide only resolved, exactly bound UI hints without editing history."""
    output = content or ""
    for call in calls or ():
        state = effective_call_state(call, states)
        call_id = call.get("tool_call_id")
        if state is not None and state.verified and not state.unknown and call_id:
            reference = f"[{UNKNOWN_CODE}] toolCallId={call_id}"
            # Whole-line matching prevents one call ID being a prefix of another.
            output = "".join(
                line
                for line in output.splitlines(keepends=True)
                if line.strip() != reference
            )
    if (
        f"[{UNKNOWN_CODE}] toolCallId=" not in output
        and not any(
            (state := effective_call_state(call, states)) and state.unknown
            for call in calls or ()
        )
        and output != (content or "")
    ):
        output = output.replace(NOTICE, "")
    return output.strip() if output != (content or "") else content


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
        identifiers_set = set()
        if isinstance(tool_call_ids, (str, bytes)):
            return {"": UNKNOWN_COMPLETION}
        for count, value in enumerate(tool_call_ids or (), 1):
            if count > 256 or not isinstance(value, str) or len(value) > 256:
                return {"": UNKNOWN_COMPLETION}
            if value:
                identifiers_set.add(value)
        identifiers = tuple(sorted(identifiers_set))
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
                    AiConversation.tenant_id
                    == func.coalesce(
                        AiPreparedAction.tenant_id, AiOperationLog.tenant_id
                    ),
                    AiConversation.conversation_id
                    == func.coalesce(
                        AiPreparedAction.conversation_id, AiOperationLog.conversation_id
                    ),
                ),
            )
            .outerjoin(
                AiMessage,
                and_(
                    AiMessage.tenant_id
                    == func.coalesce(
                        AiPreparedAction.tenant_id, AiOperationLog.tenant_id
                    ),
                    AiMessage.message_id
                    == func.coalesce(
                        AiPreparedAction.source_user_message_id,
                        AiOperationLog.source_user_message_id,
                    ),
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
                    await db.execute(
                        text(
                            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                        )
                    )
                    rows = (await db.execute(statement.limit(16385))).all()
                    if len(rows) > 16384:
                        return {"": UNKNOWN_COMPLETION}
                    from app.modules.system.service.authorization_read_budget import (  # noqa: PLC0415
                        _size,
                    )

                    if sum(_size(row) for row in rows) > 64 * 1024 * 1024:
                        return {"": UNKNOWN_COMPLETION}
                    projected = {}
                    for action, log, conversation, source in rows:
                        if log is not None and has_annotation(log.result_summary):
                            projected[log.tool_call_id] = RECONCILIATION_COMPLETION
                            continue
                        if not self._protected(action, log):
                            continue
                        call_id = (
                            action.execute_tool_call_id if action else log.tool_call_id
                        )
                        if action is None:
                            from app.modules.ai.service.preflight_completion_service import (  # noqa: PLC0415
                                is_verified_preflight,
                            )

                            if await is_verified_preflight(
                                db,
                                log,
                                conversation,
                                source,
                                tenant_id=tenant.tenant_id,
                                user_id=log.user_id,
                            ):
                                projected[call_id] = EffectiveCompletion(
                                    log.status,
                                    log.error_code,
                                    log.finished_at,
                                    log.duration_ms,
                                    verified=True,
                                )
                                continue
                        if action is None and log.tool_name == "user.import_preview":
                            from app.modules.ai.service.preview_completion_service import (  # noqa: PLC0415
                                verify_historical_preview,
                            )

                            try:
                                await verify_historical_preview(
                                    db,
                                    log,
                                    conversation,
                                    source,
                                    tenant_id=tenant.tenant_id,
                                    user_id=log.user_id,
                                )
                                projected[call_id] = EffectiveCompletion(
                                    log.status,
                                    log.error_code,
                                    log.finished_at,
                                    log.duration_ms,
                                    verified=True,
                                )
                            except (BusinessRuleException, TypeError, ValueError):
                                projected[call_id] = UNKNOWN_COMPLETION
                        else:
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
        from app.modules.ai.service.preflight_completion_service import (  # noqa: PLC0415
            has_preflight_marker,
        )

        if log is not None and has_preflight_marker(
            getattr(log, "result_summary", None)
        ):
            return True
        if log is not None and has_annotation(getattr(log, "result_summary", None)):
            return True
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
        if log is not None and has_annotation(log.result_summary):
            return RECONCILIATION_COMPLETION
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
        # A proven rejection may have a legacy NULL audit error code. Preserve
        # the validated PA decision instead of erasing USER_REJECTED on replay.
        error_code = (
            action.error_code if action.status == "rejected" else log.error_code
        )
        return EffectiveCompletion(
            log.status, error_code, log.finished_at, log.duration_ms, verified=True
        )


completion_read_state_service = CompletionReadStateService()
