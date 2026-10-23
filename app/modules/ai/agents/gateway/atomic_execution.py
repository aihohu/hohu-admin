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
from app.core.file_storage import ArtifactExecution
from app.core.tenant import TenantContext
from app.db.session import SESSION_QUARANTINED_KEY, AsyncSessionLocal
from app.modules.ai.agents.gateway.failures import clear_failures, record_failure
from app.modules.ai.agents.gateway.quota import with_l3_timeout
from app.modules.ai.agents.gateway.result import ResultProjection, ToolResult, UIResult
from app.modules.ai.agents.gateway.sensitive import serialize_for_llm
from app.modules.ai.agents.hitl.constants import PreparedActionStatus
from app.modules.ai.core.context import ChatDeps, build_tool_context
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    ClaimBinding,
    atomic_completion_service,
)
from app.modules.ai.service.completion_recovery_service import (
    RecoveryOutcome,
    completion_recovery_service,
)
from app.modules.ai.service.execution_authorization_service import (
    ensure_current_write_authority,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.user_delete_dependency_service import (
    UserDeleteDependencyChanged,
    user_delete_dependency_service,
)

logger = logging.getLogger(__name__)

OUTCOME_UNKNOWN_MESSAGE = "操作结果尚待核对，请勿重新办理；请刷新会话或联系管理员核对。"
_SESSION_CLEANUP_TIMEOUT_SEC = 0.25
_COMMIT_TIMEOUT_SEC = 8
_COMMIT_TASK_KEY = "prepared_completion_commit_task"
_DISCARDED_SESSION_KEY = SESSION_QUARANTINED_KEY
# Retain uncertain sessions until the public invalidate operation completes.
# They must not be returned to the pool by an ordinary close after a timeout.
_QUARANTINED_SESSIONS: dict[int, tuple[Any, asyncio.Task]] = {}


class PreparedExecutionOutcomeUnknown(BusinessRuleException):
    """Stop this agent turn; this signal must never become a failed tool result."""

    def __init__(self) -> None:
        super().__init__(
            OUTCOME_UNKNOWN_MESSAGE, error_code="AI_EXECUTION_OUTCOME_UNKNOWN"
        )


class _RejectedResult(Exception):
    def __init__(self, result: ToolResult) -> None:
        self.result = result


async def _commit_with_budget(db: Any) -> None:
    """A silent transport must not hold a completion request indefinitely.

    Timeout still means unknown: callers quarantine the connection and reconcile
    the original action. It is never proof that PostgreSQL rolled back.
    """
    task = asyncio.create_task(db.commit())
    db.info[_COMMIT_TASK_KEY] = task
    try:
        done, _ = await asyncio.wait({task}, timeout=_COMMIT_TIMEOUT_SEC)
        if task not in done:
            raise TimeoutError("Prepared completion COMMIT acknowledgement timed out")
        task.result()
    except BaseException:
        if not task.done():
            task.cancel()
        raise
    finally:
        if task.done():
            db.info.pop(_COMMIT_TASK_KEY, None)


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
    outcome = await reconcile_prepared_completion(
        claim.confirmation_id,
        tenant=deps.tenant,
        expected_claim=claim,
        allow_fence=True,
    )
    if outcome.disposition == "unknown":
        raise PreparedExecutionOutcomeUnknown()
    return _persisted_result(outcome.action)


async def _read_terminal_outcome(
    confirmation_id: str,
    *,
    tenant: TenantContext,
    expected_claim: ClaimBinding | None,
) -> RecoveryOutcome:
    """Fact-only second read after recovery COMMIT uncertainty; never fence again."""
    try:
        async with asyncio.timeout(8), _owned_session() as db:
            await db.begin()
            if expected_claim is not None:
                locked = await atomic_completion_service.lock_claim(
                    db,
                    expected_claim,
                    tenant=tenant,
                    require_active=False,
                    require_running=False,
                )
            else:
                locked = await atomic_completion_service.lock_terminal(
                    db, confirmation_id, tenant=tenant
                )
            action = locked.action
            if action.status == "succeeded" or (
                action.status == "failed" and action.execute_tool_name in DB_TOOLS
            ):
                return RecoveryOutcome(action, disposition=action.status)
    except (Exception, asyncio.CancelledError):
        pass
    return RecoveryOutcome(None)


async def reconcile_prepared_completion(
    confirmation_id: str,
    *,
    tenant: TenantContext,
    expected_claim: ClaimBinding | None = None,
    allow_fence: bool = False,
) -> RecoveryOutcome:
    """Own the bounded recovery transaction, with no handler or retry callback."""
    try:
        async with asyncio.timeout(8):
            return await _reconcile_prepared_completion(
                confirmation_id,
                tenant=tenant,
                expected_claim=expected_claim,
                allow_fence=allow_fence,
            )
    except (Exception, asyncio.CancelledError):
        return RecoveryOutcome(None)


async def _reconcile_prepared_completion(
    confirmation_id: str,
    *,
    tenant: TenantContext,
    expected_claim: ClaimBinding | None,
    allow_fence: bool,
) -> RecoveryOutcome:
    commit_uncertain = False
    try:
        async with asyncio.timeout(8), _owned_session() as db:
            try:
                await db.begin()
                outcome = await completion_recovery_service.reconcile(
                    db,
                    confirmation_id,
                    tenant=tenant,
                    expected_claim=expected_claim,
                    allow_fence=allow_fence,
                )
                if not outcome.changed:
                    return outcome
                await db.flush()
            except (Exception, asyncio.CancelledError):
                await _discard_uncertain_session(db)
                return RecoveryOutcome(None)
            try:
                await _commit_with_budget(db)
                return outcome
            except asyncio.CancelledError:
                await _discard_uncertain_session(db)
                return RecoveryOutcome(None)
            except Exception:
                commit_uncertain = True
                await _discard_uncertain_session(db)
    except (Exception, asyncio.CancelledError):
        return RecoveryOutcome(None)
    if commit_uncertain:
        return await _read_terminal_outcome(
            confirmation_id, tenant=tenant, expected_claim=expected_claim
        )
    return RecoveryOutcome(None)


async def _discard_uncertain_session(db: Any) -> None:
    # rollback after a COMMIT error is not evidence that COMMIT did not happen.
    if db.info.get(_DISCARDED_SESSION_KEY):
        return
    db.info[_DISCARDED_SESSION_KEY] = True
    pending_commit = db.info.get(_COMMIT_TASK_KEY)

    async def invalidate_when_idle() -> None:
        # Driver cancellation may itself wait for network teardown. Keep that
        # task and session quarantined; never invalidate concurrently with it or
        # let its teardown extend the response budget.
        if pending_commit is not None:
            await asyncio.gather(pending_commit, return_exceptions=True)
            db.info.pop(_COMMIT_TASK_KEY, None)
        await db.invalidate()

    task = asyncio.create_task(invalidate_when_idle())
    key = id(db)
    _QUARANTINED_SESSIONS[key] = (db, task)

    def invalidation_finished(completed: asyncio.Task) -> None:
        try:
            completed.result()
        except (Exception, asyncio.CancelledError):
            # A failed invalidation is not permission to pool an uncertain
            # connection. Keep the session isolated for operational recovery.
            logger.warning("prepared session remains quarantined", exc_info=True)
        else:
            _QUARANTINED_SESSIONS.pop(key, None)

    task.add_done_callback(invalidation_finished)
    with suppress(Exception, asyncio.CancelledError):
        async with asyncio.timeout(_SESSION_CLEANUP_TIMEOUT_SEC):
            await asyncio.shield(task)


@asynccontextmanager
async def _owned_session():
    db = AsyncSessionLocal()
    try:
        yield db
    finally:
        if not db.info.get(_DISCARDED_SESSION_KEY):
            try:
                async with asyncio.timeout(_SESSION_CLEANUP_TIMEOUT_SEC):
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
            await _commit_with_budget(db)
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
    for attempt in range(2):
        try:
            return await _execute_claim_attempt(
                claim,
                deps,
                validate_execution=validate_execution,
                retry_preflight=attempt == 0,
            )
        except UserDeleteDependencyChanged:
            if attempt:
                raise
    raise PreparedExecutionOutcomeUnknown()


async def _execute_claim_attempt(
    claim: ClaimBinding,
    deps: ChatDeps,
    *,
    validate_execution: Callable[..., Any],
    retry_preflight: bool,
) -> ToolResult:
    started = time.monotonic()
    result: ToolResult | None = None
    rolled_back = False
    cancelled = False
    handler_started = False
    target_ids = None
    async with _owned_session() as db:
        try:
            await db.begin()
            if claim.execute_tool_name == "user.batch_delete":
                from app.modules.system.service.user_service import (  # noqa: PLC0415
                    user_service,
                )

                reference = await prepared_action_service.get_by_confirmation_id(
                    db, claim.confirmation_id, tenant=deps.tenant
                )
                if reference is None or not isinstance(reference.frozen_args, dict):
                    raise PreparedExecutionOutcomeUnknown()
                raw_ids = reference.frozen_args.get("user_ids")
                if (
                    not isinstance(raw_ids, list)
                    or not raw_ids
                    or any(
                        isinstance(value, bool)
                        or not str(value).isdigit()
                        or int(value) <= 0
                        for value in raw_ids
                    )
                ):
                    raise BusinessRuleException(
                        "删除操作缺少冻结的用户目标",
                        error_code="AI_PREPARED_ACTION_BINDING_INVALID",
                    )
                target_ids = [int(value) for value in raw_ids]
                plan = await user_delete_dependency_service.plan(
                    db,
                    target_user_ids=target_ids,
                    tenant=deps.tenant,
                    own_claim=claim,
                )
                await user_delete_dependency_service.lock_plan(
                    db, plan, tenant=deps.tenant
                )
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
            if target_ids is not None:
                await user_service.prepare_delete_dependencies(
                    db, target_ids, tenant=deps.tenant
                )
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
            handler_started = True
            ctx.artifact_execution = ArtifactExecution(
                claim.tenant_id, str(claim.action_id), claim.execution_owner
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
            if claim.execute_tool_name in {
                "user.import_preview",
                "user.import_execute",
                "user.export",
            }:
                from app.modules.system.service.user_transfer_artifact_service import (  # noqa: PLC0415
                    validate_file_completion,
                )

                await validate_file_completion(
                    db, action=action, result=result, execution=ctx.artifact_execution
                )
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
            if (
                isinstance(exc, UserDeleteDependencyChanged)
                and retry_preflight
                and not handler_started
            ):
                raise
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
                await _commit_with_budget(db)
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
