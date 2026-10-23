"""Tenant-scoped AI Trace audit projections."""

import json
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.core.exceptions import NotFoundException
from app.core.tenant import TenantContext
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.schemas.operation_log import (
    TraceDetailOut,
    TraceListQuery,
    TraceOperationOut,
    TraceSummaryOut,
    TraceTargetOut,
)
from app.modules.ai.service.completion_read_state_service import (
    completion_read_state_service,
    effective_log_state,
)
from app.modules.system.service.user_service import user_service


def _parse_target_summary(value: str | None) -> list[TraceTargetOut]:
    """Parse stored allowlisted targets and fail closed on malformed legacy data."""
    if value is None:
        return []
    try:
        payload = json.loads(value)
        if not isinstance(payload, list):
            return []
        return [TraceTargetOut.model_validate(item) for item in payload]
    except (TypeError, ValueError):
        return []


class TraceService:
    """Build strict audit DTOs without loading message or argument content."""

    @staticmethod
    def _filters(query: TraceListQuery, tenant: TenantContext) -> list[Any]:
        filters: list[Any] = [AiOperationLog.tenant_id == tenant.tenant_id]
        if query.trace_id is not None:
            filters.append(AiOperationLog.trace_id == query.trace_id)
        if query.actor_id is not None:
            filters.append(AiOperationLog.user_id == query.actor_id)
        if query.agent_code is not None:
            filters.append(AiOperationLog.agent_code == query.agent_code)
        if query.tool_name is not None:
            filters.append(AiOperationLog.tool_name == query.tool_name)
        if query.queued_from is not None:
            filters.append(
                AiOperationLog.queued_at >= query.queued_from.replace(tzinfo=None)
            )
        if query.queued_to is not None:
            filters.append(
                AiOperationLog.queued_at <= query.queued_to.replace(tzinfo=None)
            )
        return filters

    async def list_traces(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        query: TraceListQuery,
    ) -> PageResult[TraceSummaryOut]:
        filters = self._filters(query, tenant)
        grouped = (
            select(
                AiOperationLog.trace_id.label("trace_id"),
                func.max(AiOperationLog.queued_at).label("latest_queued_at"),
                func.max(AiOperationLog.log_id).label("latest_log_id"),
            )
            .where(*filters)
            .group_by(AiOperationLog.trace_id)
        )
        projected_states = None
        filtered_rows = None
        if query.status is None:
            total = int(
                (
                    await db.execute(
                        select(func.count()).select_from(grouped.subquery())
                    )
                ).scalar_one()
            )
            page_rows = (
                await db.execute(
                    grouped.order_by(
                        func.max(AiOperationLog.queued_at).desc(),
                        func.max(AiOperationLog.log_id).desc(),
                    )
                    .offset((query.current - 1) * query.size)
                    .limit(query.size)
                )
            ).all()
            trace_ids = [str(row.trace_id) for row in page_rows]
        else:
            # The full proof cannot be represented by raw status SQL. Select
            # candidate traces using the other filters, then classify all their
            # operations in one batch before counting and paginating matches.
            matching_ids = set(
                await db.scalars(select(AiOperationLog.log_id).where(*filters))
            )
            candidate_traces = select(AiOperationLog.trace_id).where(*filters)
            candidate_rows = list(
                await db.scalars(
                    select(AiOperationLog)
                    .where(
                        AiOperationLog.tenant_id == tenant.tenant_id,
                        AiOperationLog.trace_id.in_(candidate_traces),
                    )
                    .order_by(AiOperationLog.queued_at, AiOperationLog.log_id)
                )
            )
            projected_states = await completion_read_state_service.read(
                tenant=tenant,
                tool_call_ids=[row.tool_call_id for row in candidate_rows],
            )
            latest_matches = {}
            for operation in candidate_rows:
                if (
                    operation.log_id in matching_ids
                    and effective_log_state(operation, projected_states).status
                    == query.status
                ):
                    latest_matches[operation.trace_id] = (
                        operation.queued_at,
                        operation.log_id,
                    )
            ordered = sorted(
                latest_matches, key=latest_matches.__getitem__, reverse=True
            )
            total = len(ordered)
            start = (query.current - 1) * query.size
            trace_ids = ordered[start : start + query.size]
            page_trace_ids = set(trace_ids)
            filtered_rows = [
                row for row in candidate_rows if row.trace_id in page_trace_ids
            ]
        if not trace_ids:
            return PageResult(
                records=[],
                total=total,
                current=query.current,
                size=query.size,
            )

        rows = (
            filtered_rows
            if filtered_rows is not None
            else list(
                (
                    await db.execute(
                        select(AiOperationLog)
                        .where(
                            AiOperationLog.tenant_id == tenant.tenant_id,
                            AiOperationLog.trace_id.in_(trace_ids),
                        )
                        .order_by(AiOperationLog.queued_at, AiOperationLog.log_id)
                    )
                )
                .scalars()
                .all()
            )
        )
        if projected_states is None:
            projected_states = await completion_read_state_service.read(
                tenant=tenant, tool_call_ids=[row.tool_call_id for row in rows]
            )
        actor_names = await user_service.get_user_names_by_ids(
            db,
            {operation.user_id for operation in rows},
            tenant=tenant,
        )
        by_trace: dict[str, list[AiOperationLog]] = defaultdict(list)
        for operation in rows:
            by_trace[str(operation.trace_id)].append(operation)

        records: list[TraceSummaryOut] = []
        for trace_id in trace_ids:
            operations = by_trace[trace_id]
            latest = operations[-1]
            effective = [
                effective_log_state(operation, projected_states)
                for operation in operations
            ]
            finished_values = [
                state.finished_at
                for state in effective
                if state.finished_at is not None
            ]
            records.append(
                TraceSummaryOut(
                    traceId=trace_id,
                    actorId=latest.user_id,
                    actorName=actor_names.get(latest.user_id, "unknown"),
                    agentCodes=sorted(
                        {operation.agent_code or "unknown" for operation in operations}
                    ),
                    toolNames=sorted({operation.tool_name for operation in operations}),
                    statuses=sorted({state.status for state in effective}),
                    operationCount=len(operations),
                    queuedAt=operations[0].queued_at,
                    finishedAt=max(finished_values)
                    if finished_values and not any(state.unknown for state in effective)
                    else None,
                )
            )
        return PageResult(
            records=records,
            total=total,
            current=query.current,
            size=query.size,
        )

    async def get_trace(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        trace_id: str,
    ) -> TraceDetailOut:
        rows = (
            await db.execute(
                select(
                    AiOperationLog,
                    AiMessage.role,
                    AiMessage.create_time,
                )
                .outerjoin(
                    AiMessage,
                    (AiMessage.tenant_id == AiOperationLog.tenant_id)
                    & (AiMessage.message_id == AiOperationLog.source_user_message_id),
                )
                .where(
                    AiOperationLog.tenant_id == tenant.tenant_id,
                    AiOperationLog.trace_id == trace_id,
                )
                .order_by(AiOperationLog.queued_at, AiOperationLog.log_id)
            )
        ).all()
        if not rows:
            raise NotFoundException(
                "AI Trace",
                error_code="AI_TRACE_NOT_FOUND",
            )
        actor_names = await user_service.get_user_names_by_ids(
            db,
            {operation.user_id for operation, _source_role, _source_at in rows},
            tenant=tenant,
        )
        states = await completion_read_state_service.read(
            tenant=tenant,
            tool_call_ids=[operation.tool_call_id for operation, _, _ in rows],
        )
        effective = {
            operation.log_id: effective_log_state(operation, states)
            for operation, _, _ in rows
        }

        operations = [
            TraceOperationOut(
                logId=operation.log_id,
                toolCallId=operation.tool_call_id,
                toolName=operation.tool_name,
                agentCode=operation.agent_code or "unknown",
                actorId=operation.user_id,
                actorName=actor_names.get(operation.user_id, "unknown"),
                sourceMessageId=operation.source_user_message_id,
                sourceMessageRole=source_role,
                sourceMessageAt=source_created_at,
                targetSummary=_parse_target_summary(operation.target_summary),
                executionMode=operation.execution_mode,
                riskLevel=operation.risk_level,
                status=effective[operation.log_id].status,
                errorCode=effective[operation.log_id].error_code,
                confirmationId=operation.confirmation_id,
                approvedBy=operation.approved_by,
                queuedAt=operation.queued_at,
                startedAt=operation.started_at,
                finishedAt=effective[operation.log_id].finished_at,
                durationMs=effective[operation.log_id].duration_ms,
                hitlWaitMs=operation.hitl_wait_ms,
            )
            for operation, source_role, source_created_at in rows
        ]
        return TraceDetailOut(
            traceId=trace_id,
            conversationId=rows[0][0].conversation_id,
            operations=operations,
        )


trace_service = TraceService()
