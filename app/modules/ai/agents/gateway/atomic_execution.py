"""Gateway-owned transactions for a durable prepared-action claim."""

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from typing import Any

from app.core import redis as redis_module
from app.core.exceptions import BusinessException, BusinessRuleException
from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.gateway.failures import clear_failures, record_failure
from app.modules.ai.agents.gateway.quota import with_l3_timeout
from app.modules.ai.agents.gateway.result import ResultProjection, ToolResult, UIResult
from app.modules.ai.agents.gateway.sensitive import serialize_for_llm
from app.modules.ai.agents.hitl.constants import PreparedActionStatus
from app.modules.ai.core.context import ChatDeps, build_tool_context
from app.modules.ai.service.atomic_completion_service import (
    ClaimBinding,
    atomic_completion_service,
)
from app.modules.ai.service.execution_authorization_service import (
    ensure_current_write_authority,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service

logger = logging.getLogger(__name__)

OUTCOME_UNKNOWN_MESSAGE = "操作结果尚待核对，请勿重新办理；请刷新会话或联系管理员核对。"


class PreparedExecutionOutcomeUnknown(BusinessRuleException):
    """Stop this agent turn; this signal must never become a failed tool result."""

    def __init__(self) -> None:
        super().__init__(
            OUTCOME_UNKNOWN_MESSAGE, error_code="AI_EXECUTION_OUTCOME_UNKNOWN"
        )


class _RejectedResult(Exception):
    def __init__(self, result: ToolResult) -> None:
        self.result = result


def _persisted_result(action: Any) -> ToolResult:
    if action.status == PreparedActionStatus.SUCCEEDED.value:
        value = action.result_ui
        ui = (
            UIResult(
                view_type=value.get("viewType", "plain_json"),
                view_data=value.get("viewData", {}),
                audit=value.get("audit", {}),
                label_key=value.get("labelKey", ""),
                label_params=value.get("labelParams", {}),
            )
            if value
            else None
        )
        return ToolResult.success(
            action.result_data,
            ui=ui,
            projection=ResultProjection(
                subject_refs=tuple(action.subject_refs or ()),
                scope_bound=action.data_scope_hash is not None,
            ),
        )
    if action.status == PreparedActionStatus.FAILED.value:
        return ToolResult.failure(
            action.error_code or "AI_INTERNAL_ERROR", "操作未完成，业务修改已回滚。"
        )
    raise PreparedExecutionOutcomeUnknown()


async def _read_committed_result(claim: ClaimBinding, deps: ChatDeps) -> ToolResult:
    """Only positive, fully bound terminal facts can resolve an uncertain commit."""
    try:
        async with asyncio.timeout(8), _owned_session() as db:
            await db.begin()
            locked = await atomic_completion_service.lock_claim(
                db,
                claim,
                tenant=deps.tenant,
                require_active=False,
                require_running=False,
            )
            return _persisted_result(locked.action)
    except (Exception, asyncio.CancelledError) as exc:
        raise PreparedExecutionOutcomeUnknown() from exc


async def _discard_uncertain_session(db: Any) -> None:
    # rollback after a COMMIT error is not evidence that COMMIT did not happen.
    with suppress(Exception, asyncio.CancelledError):
        await db.invalidate()


@asynccontextmanager
async def _owned_session():
    db = AsyncSessionLocal()
    try:
        yield db
    finally:
        try:
            await db.close()
        except (Exception, asyncio.CancelledError):
            await _discard_uncertain_session(db)
            logger.warning("prepared session cleanup unavailable", exc_info=True)


async def _finish_rolled_back(
    claim: ClaimBinding,
    deps: ChatDeps,
    result: ToolResult,
    duration_ms: int,
) -> ToolResult:
    if not claim.execution_owner.startswith("v0162-db:"):
        # File side-effect disposition belongs to the file adapter, not DB absence.
        raise PreparedExecutionOutcomeUnknown()
    async with _owned_session() as db:
        try:
            await db.begin()
            locked = await atomic_completion_service.lock_claim(
                db,
                claim,
                tenant=deps.tenant,
                require_active=False,
                require_running=False,
            )
            if locked.action.status != PreparedActionStatus.RUNNING.value:
                return _persisted_result(locked.action)
            await atomic_completion_service.complete_failed(
                db,
                locked,
                error_code=result.error_code or "AI_INTERNAL_ERROR",
                duration_ms=duration_ms,
                tenant=deps.tenant,
            )
            await db.flush()
        except (Exception, asyncio.CancelledError) as exc:
            await _discard_uncertain_session(db)
            raise PreparedExecutionOutcomeUnknown() from exc
        try:
            await db.commit()
        except (Exception, asyncio.CancelledError):
            await _discard_uncertain_session(db)
            return await _read_committed_result(claim, deps)
    return result


async def execute_claim(
    claim: ClaimBinding,
    deps: ChatDeps,
    *,
    validate_execution: Callable[..., Any],
) -> ToolResult:
    """Commit business, PA and required audit together; never retry the handler."""
    started = time.monotonic()
    result: ToolResult | None = None
    rolled_back = False
    cancelled = False
    async with _owned_session() as db:
        try:
            await db.begin()
            locked = await atomic_completion_service.lock_claim(
                db, claim, tenant=deps.tenant
            )
            action = locked.action
            registered = validate_execution(action, deps)
            meta = registered.meta
            await prepared_action_service.validate_snapshot(
                db, action, tenant=deps.tenant
            )
            prepared_action_service.validate_data_scope_snapshot(
                action, current_data_scope_hash=deps.data_scope_hash
            )
            await ensure_current_write_authority(db, deps=deps, meta=meta)
            snapshot = (
                action.snapshot.get("business")
                if isinstance(action.snapshot, dict)
                else None
            )
            ctx = build_tool_context(
                deps,
                db,
                meta,
                approved_business_snapshot=deepcopy(snapshot)
                if isinstance(snapshot, dict)
                else None,
            )
            raw = await with_l3_timeout(
                registered.fn(ctx, **deepcopy(action.frozen_args)), tenant=deps.tenant
            )
            result = raw if isinstance(raw, ToolResult) else ToolResult.success(raw)
            if not result.ok:
                raise _RejectedResult(result)
            result.data = serialize_for_llm(meta.sensitive_output, result.data)
            if result.projection is None and meta.projection_kind is not None:
                result.projection = ResultProjection(
                    scope_bound=meta.projection_kind == "scope_bound"
                )
            await db.flush()
            await ensure_current_write_authority(db, deps=deps, meta=meta)
            await atomic_completion_service.complete_success(
                db,
                locked,
                result=result,
                deps=deps,
                duration_ms=int((time.monotonic() - started) * 1000),
                tenant=deps.tenant,
            )
            await db.flush()
        except (Exception, asyncio.CancelledError) as exc:
            try:
                await db.rollback()
            except (Exception, asyncio.CancelledError) as rollback_error:
                await _discard_uncertain_session(db)
                raise PreparedExecutionOutcomeUnknown() from rollback_error
            rolled_back = True
            cancelled = isinstance(exc, asyncio.CancelledError)
            if isinstance(exc, _RejectedResult):
                result = exc.result
            elif isinstance(exc, BusinessException):
                result = ToolResult.failure(
                    exc.error_code or "AI_INTERNAL_ERROR", exc.message
                )
            else:
                logger.warning("prepared transaction body rolled back", exc_info=True)
                result = ToolResult.failure(
                    "AI_INTERNAL_ERROR", "操作未完成，业务修改已回滚。"
                )
        if not rolled_back:
            try:
                await db.commit()
            except (Exception, asyncio.CancelledError):
                await _discard_uncertain_session(db)
                return await _read_committed_result(claim, deps)

    assert result is not None
    if rolled_back:
        result = await _finish_rolled_back(
            claim, deps, result, int((time.monotonic() - started) * 1000)
        )
    # Auxiliary Redis bookkeeping cannot change a committed business outcome.
    try:
        operation = clear_failures if result.ok else record_failure
        await operation(
            redis_module.redis_client,
            deps.user.user_id,
            claim.execute_tool_name,
            claim.args_hash,
            tenant=deps.tenant,
        )
    except Exception:
        logger.warning(
            "prepared completion auxiliary bookkeeping unavailable", exc_info=True
        )
    if cancelled:
        raise asyncio.CancelledError
    return result
