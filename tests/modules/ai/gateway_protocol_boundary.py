"""Test-only adapters for synthetic tool protocol/transport fixtures.

These suites use MagicMock users and tools that do not exist in production.
They test events, quotas and HITL orchestration, not JWT/lock-plan enforcement.
The real fixed owners are covered by test_authorization_gateway_* with JWTs.
"""

import time

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.hitl.constants import AiExecutionMode, AiOperationStatus
from app.modules.ai.core.context import build_tool_context
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.preflight_completion_service import (
    runtime_preflight_summary,
)


def install_protocol_boundaries(monkeypatch):
    async def start(
        deps,
        registered,
        tool_call_id,
        args_hash,
        args_summary,
        mode,
        *,
        preflight_failure=None,
    ):
        async with AsyncSessionLocal() as db, db.begin():
            log_id = await operation_log_service.start_operation(
                db,
                tenant=deps.tenant,
                user_id=deps.user.user_id,
                conversation_id=deps.conversation_id,
                source_user_message_id=deps.source_user_message_id,
                trace_id=deps.trace_id,
                agent_code=deps.agent.code,
                readonly_snapshot=registered.meta.readonly,
                tool_name=registered.meta.name,
                tool_call_id=tool_call_id,
                args_hash=args_hash,
                args_summary=args_summary,
                risk_level=registered.meta.risk,
                execution_mode=mode.value,
                status=AiOperationStatus.RUNNING
                if mode == AiExecutionMode.AUTONOMOUS and preflight_failure is None
                else AiOperationStatus.PENDING_CONFIRMATION,
                is_security_event=deps.injection_hit,
                event_type="injection_pattern_matched" if deps.injection_hit else None,
            )
            if preflight_failure is not None:
                log = await operation_log_service.mark_failed(
                    db,
                    log_id,
                    tenant=deps.tenant,
                    duration_ms=0,
                    error_code=preflight_failure.error_code or "AI_INTERNAL_ERROR",
                )
                if not registered.meta.readonly:
                    log.result_summary = runtime_preflight_summary(log)
            return log_id

    async def finish(log_id, result, started_at, *, tenant):
        async with AsyncSessionLocal() as db, db.begin():
            log = await db.get(AiOperationLog, log_id)
            action = await db.scalar(
                select(AiPreparedAction.action_id).where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.execute_tool_call_id == log.tool_call_id,
                )
            )
            if action is not None or AiOperationStatus(log.status).is_terminal:
                return
            duration = max(0, int((time.monotonic() - started_at) * 1000))
            if result.ok:
                await operation_log_service.mark_success(
                    db,
                    log_id,
                    tenant=tenant,
                    result_summary="ok",
                    duration_ms=duration,
                )
            else:
                await operation_log_service.mark_failed(
                    db,
                    log_id,
                    tenant=tenant,
                    error_code=result.error_code or "AI_INTERNAL_ERROR",
                    duration_ms=duration,
                )

    async def read(registered, args, deps):
        async with AsyncSessionLocal() as db:
            result = await registered.fn(
                build_tool_context(deps, db, registered.meta), **args
            )
            return (
                result if isinstance(result, ToolResult) else ToolResult.success(result)
            )

    async def preflight(registered, args, deps):
        async with AsyncSessionLocal() as db:
            return await registered.dry_run_fn(
                build_tool_context(deps, db, registered.meta), **args
            )

    monkeypatch.setattr(executor, "_preflight_tool", preflight)
    monkeypatch.setattr(executor, "_start_log", start)
    monkeypatch.setattr(executor, "_finish_log_final", finish)
    monkeypatch.setattr(executor, "_read_tool", read)
