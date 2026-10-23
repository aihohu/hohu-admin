"""Own the single preview transaction; recovery can only read success facts."""

import asyncio
import time
from copy import deepcopy

from app.core.file_storage import ArtifactExecution
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
    _commit_with_budget,
    _discard_uncertain_session,
    _owned_session,
)
from app.modules.ai.agents.gateway.quota import with_l3_timeout
from app.modules.ai.agents.gateway.result import ResultProjection, ToolResult
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.core.context import build_tool_context
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.execution_authorization_service import (
    ensure_current_write_authority,
)
from app.modules.ai.service.preview_completion_service import (
    complete_preview,
    lock_preview,
    preview_lineage,
    verify_historical_preview,
)
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import load_live_user_authority


async def read_preview_result(*, log_id, tool_call_id, args_hash, deps):
    """Never recreate a proposal, create a PA, invoke a handler, or delete files."""
    try:
        async with asyncio.timeout(8), _owned_session() as db:
            await db.begin()
            log = await lock_preview(
                db,
                log_id=log_id,
                tool_call_id=tool_call_id,
                args_hash=args_hash,
                deps=deps,
            )
            registered = ToolRegistry.get().find("user.import_preview")
            if registered is None:
                raise PreparedExecutionOutcomeUnknown()
            await ensure_current_write_authority(db, deps=deps, meta=registered.meta)
            audit = await verify_historical_preview(
                db,
                log,
                await db.get(AiConversation, deps.conversation_id),
                await db.get(AiMessage, deps.source_user_message_id),
                tenant_id=deps.tenant_id,
                user_id=deps.user.user_id,
            )
            if audit.get("data_scope_hash") != deps.data_scope_hash:
                raise PreparedExecutionOutcomeUnknown()
            current = await load_live_user_authority(db, tenant=deps.tenant)
            visible = await result_projection_service.authorize_result_projection(
                db,
                current,
                owner_user_id=log.user_id,
                lineage=preview_lineage(log, audit, deps.tenant),
            )
            data = {"batchId": audit["batch_id"]}
            if visible:
                data.update(total=audit["total"], summary=audit["summary"])
            # A minimal receipt has no download URL, token, frozen args or
            # executable proposal. Current projection still owns visible data.
            return ToolResult.success(
                data,
                projection=ResultProjection(
                    subject_refs=(
                        {"type": "user_import_batch", "id": audit["batch_id"]},
                    ),
                    scope_bound=True,
                ),
                preview_replay=True,
            )
    except (Exception, asyncio.CancelledError) as exc:
        raise PreparedExecutionOutcomeUnknown() from exc


async def execute_preview(registered, args, deps, *, log_id, tool_call_id, args_hash):
    started = time.monotonic()
    async with _owned_session() as db:
        try:
            await db.begin()
            log = await lock_preview(
                db,
                log_id=log_id,
                tool_call_id=tool_call_id,
                args_hash=args_hash,
                deps=deps,
            )
            if log.status != "running":
                raise PreparedExecutionOutcomeUnknown()
            await ensure_current_write_authority(db, deps=deps, meta=registered.meta)
            ctx = build_tool_context(deps, db, registered.meta)
            ctx.artifact_execution = ArtifactExecution(
                deps.tenant_id,
                tool_call_id,
                f"preview:{log_id}",
            )
            result = await with_l3_timeout(
                registered.fn(ctx, **deepcopy(args)),
                tenant=deps.tenant,
            )
            if isinstance(result, ToolResult) and not result.ok:
                await db.rollback()
                # The real parser returns this rejection before creating any
                # batch or artifact. Other failures retain an unknown receipt.
                if result.error_code == "AI_IMPORT_FIELD_ERRORS":
                    return result
                raise PreparedExecutionOutcomeUnknown()
            await ensure_current_write_authority(db, deps=deps, meta=registered.meta)
            await complete_preview(
                db,
                log=log,
                result=result,
                args=args,
                execution=ctx.artifact_execution,
                deps=deps,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            result.meta["execute_tool_call_id"] = hitl_manager.generate_tool_call_id()
            result.meta["confirmation_id"] = hitl_manager.generate_confirmation_id()
        except (Exception, asyncio.CancelledError) as exc:
            try:
                await db.rollback()
            except (Exception, asyncio.CancelledError):
                await _discard_uncertain_session(db)
            raise PreparedExecutionOutcomeUnknown() from exc
        try:
            await _commit_with_budget(db)
        except (Exception, asyncio.CancelledError):
            await _discard_uncertain_session(db)
            return await read_preview_result(
                log_id=log_id,
                tool_call_id=tool_call_id,
                args_hash=args_hash,
                deps=deps,
            )
    return result
