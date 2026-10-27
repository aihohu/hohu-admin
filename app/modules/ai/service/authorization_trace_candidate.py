"""Bounded trace DTOs classified from one complete tenant context proof."""

from collections import defaultdict

from sqlalchemy import select

from app.core.authorization_plan import LockPlan
from app.core.base_response import PageResult
from app.core.exceptions import NotFoundException
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.operation_log import (
    TraceDetailOut,
    TraceOperationOut,
    TraceSummaryOut,
)
from app.modules.ai.service.authorization_completion_projection import (
    project_completions,
)
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.completion_read_state_service import effective_log_state
from app.modules.ai.service.trace_service import _parse_target_summary, trace_service
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.user_service import user_service


class TraceCandidate:
    async def _snapshot(self, db, tx, tenant, version, filters):
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=version,
            permissions=("ai:trace:view",),
        )
        matching = select(AiOperationLog.trace_id).where(*filters)
        statement = (
            select(AiOperationLog)
            .where(
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.trace_id.in_(matching),
            )
            .order_by(AiOperationLog.queued_at, AiOperationLog.log_id)
            .execution_options(populate_existing=True)
        )
        references = list(await db.scalars(statement))
        inventory = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            conversation_ids=tuple(sorted({r.conversation_id for r in references})),
        )
        await tx.lock(
            LockPlan(existing=tuple(set(identity.keys) | set(inventory.keys)))
        )
        await ai_context_candidate.recheck(db, tx, inventory)
        await assert_identity(db, identity)
        rows = list(await db.scalars(statement))
        # All matching rows must belong to the same discovered context set.
        if {r.log_id for r in rows} != {r.log_id for r in references}:
            from app.db.authorization_plan_session import _order_error  # noqa: PLC0415

            raise _order_error()
        cids = inventory.conversations
        actions = list(
            await db.scalars(
                select(AiPreparedAction).where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.conversation_id.in_(cids),
                )
            )
        )
        conversations = {
            r.conversation_id: r
            for r in await db.scalars(
                select(AiConversation).where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.conversation_id.in_(cids),
                )
            )
        }
        sources = {
            r.message_id: r
            for r in await db.scalars(
                select(AiMessage).where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.conversation_id.in_(cids),
                    AiMessage.role == "user",
                )
            )
        }
        states = await project_completions(
            db,
            tenant=tenant,
            actions=actions,
            logs=rows,
            conversations=conversations,
            sources=sources,
        )
        names = await user_service.get_user_names_by_ids(
            db, {r.user_id for r in rows}, tenant=tenant
        )
        return rows, states, names, sources

    async def list(self, db, tx, *, tenant, actor_user_version, query):
        filters = trace_service._filters(query, tenant)
        rows, states, names, _ = await self._snapshot(
            db, tx, tenant, actor_user_version, filters
        )
        matching = set(await db.scalars(select(AiOperationLog.log_id).where(*filters)))
        latest = {}
        grouped = defaultdict(list)
        for row in rows:
            grouped[row.trace_id].append(row)
            if row.log_id in matching and (
                query.status is None
                or effective_log_state(row, states).status == query.status
            ):
                latest[row.trace_id] = (row.queued_at, row.log_id)
        ordered = sorted(latest, key=latest.__getitem__, reverse=True)
        start = (query.current - 1) * query.size
        records = []
        for trace_id in ordered[start : start + query.size]:
            group = grouped[trace_id]
            effective = [effective_log_state(r, states) for r in group]
            finished = [s.finished_at for s in effective if s.finished_at is not None]
            records.append(
                TraceSummaryOut(
                    traceId=trace_id,
                    actorId=group[-1].user_id,
                    actorName=names.get(group[-1].user_id, "unknown"),
                    agentCodes=sorted({r.agent_code or "unknown" for r in group}),
                    toolNames=sorted({r.tool_name for r in group}),
                    statuses=sorted({s.status for s in effective}),
                    operationCount=len(group),
                    queuedAt=group[0].queued_at,
                    finishedAt=max(finished)
                    if finished and not any(s.unknown for s in effective)
                    else None,
                )
            )
        return PageResult(
            records=records, total=len(ordered), current=query.current, size=query.size
        )

    async def detail(self, db, tx, *, tenant, actor_user_version, trace_id):
        rows, states, names, sources = await self._snapshot(
            db,
            tx,
            tenant,
            actor_user_version,
            (
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.trace_id == trace_id,
            ),
        )
        if not rows:
            raise NotFoundException("AI Trace", error_code="AI_TRACE_NOT_FOUND")
        operations = []
        for row in rows:
            state = effective_log_state(row, states)
            source = sources.get(row.source_user_message_id)
            operations.append(
                TraceOperationOut(
                    logId=row.log_id,
                    toolCallId=row.tool_call_id,
                    toolName=row.tool_name,
                    agentCode=row.agent_code or "unknown",
                    actorId=row.user_id,
                    actorName=names.get(row.user_id, "unknown"),
                    sourceMessageId=row.source_user_message_id,
                    sourceMessageRole=source.role if source else None,
                    sourceMessageAt=source.create_time if source else None,
                    targetSummary=_parse_target_summary(row.target_summary),
                    executionMode=row.execution_mode,
                    riskLevel=row.risk_level,
                    status=state.status,
                    errorCode=state.error_code,
                    confirmationId=row.confirmation_id,
                    approvedBy=row.approved_by,
                    queuedAt=row.queued_at,
                    startedAt=row.started_at,
                    finishedAt=state.finished_at,
                    durationMs=state.duration_ms,
                    hitlWaitMs=row.hitl_wait_ms,
                )
            )
        return TraceDetailOut(
            traceId=trace_id,
            conversationId=rows[0].conversation_id,
            operations=operations,
        )


trace_candidate = TraceCandidate()
