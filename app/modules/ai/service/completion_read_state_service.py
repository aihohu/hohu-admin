"""Immutable read projections from one committed PA/log/context snapshot.

Unlike recovery, this reader acquires no execution locks and never infers a
rollback or fences an owner. PA proofs use one joined statement; preview proofs
also check their batch in the same REPEATABLE READ, READ ONLY snapshot.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from app.core.exceptions import BusinessRuleException
from app.modules.ai.models.operation_log import AiOperationLog
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
        self, *, tenant, tool_call_ids=None, conversation_id=None, user_id=None
    ):
        from app.db.authorization_http import get_authorization_runtime  # noqa: PLC0415

        return await get_authorization_runtime().completion_facts.read(
            tenant=tenant,
            tool_call_ids=tool_call_ids,
            conversation_id=conversation_id,
            user_id=user_id,
        )

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
