"""Bounded trace DTOs classified from one complete tenant context proof."""

from collections import defaultdict

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, ResourceKey, ResourceKind
from app.core.exceptions import NotFoundException
from app.db.authorization_plan_session import _order_error
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
from app.modules.ai.service.operation_status_projection import project_operation
from app.modules.ai.service.trace_service import _parse_target_summary, trace_service
from app.modules.auth.service import load_live_user_authority
from app.modules.system.field_policy.delivery_catalog import (
    EQ,
    GROUP,
    RANGE,
    READ,
    SORT,
)
from app.modules.system.schemas.delivery_projection import DeliveryPage
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.delivery_read_context import load_delivery_context


def fields_allowed(context, rows, name, access=READ):
    return all(
        context.decisions(
            context.record("ai.operation_log", str(row.log_id), context.scopes), access
        )[name].allowed
        for row in rows
    )


class TraceCandidate:
    async def operation(self, db, tx, *, tenant, actor_user_version, tool_call_id):
        identity = await read_identity(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
        log_query = (
            select(AiOperationLog)
            .where(
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.tool_call_id == tool_call_id,
            )
            .execution_options(populate_existing=True)
        )
        action_query = (
            select(AiPreparedAction)
            .where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.execute_tool_call_id == tool_call_id,
            )
            .execution_options(populate_existing=True)
        )
        log, action = await db.scalar(log_query), await db.scalar(action_query)
        references = tuple(row for row in (log, action) if row is not None)
        cids = tuple(
            sorted(
                {
                    row.conversation_id
                    for row in references
                    if row.conversation_id is not None
                }
            )
        )
        inventory = await ai_context_candidate.discover(
            db, tx, tenant_id=tenant.tenant_id, conversation_ids=cids
        )
        direct = tuple(
            [ResourceKey(ResourceKind.OPERATION_LOG, tenant.tenant_id, log.log_id)]
            if log
            else []
        ) + tuple(
            [
                ResourceKey(
                    ResourceKind.PREPARED_ACTION, tenant.tenant_id, action.action_id
                )
            ]
            if action
            else []
        )
        ids = (log.log_id if log else None, action.action_id if action else None)
        await tx.lock(
            LockPlan(
                existing=tuple(set(identity.keys) | set(inventory.keys) | set(direct))
            )
        )
        await ai_context_candidate.recheck(db, tx, inventory)
        await assert_identity(db, identity)
        log, action = await db.scalar(log_query), await db.scalar(action_query)
        if ids != (log.log_id if log else None, action.action_id if action else None):
            raise _order_error()
        actions = list(
            await db.scalars(
                select(AiPreparedAction).where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.conversation_id.in_(cids),
                )
            )
        )
        logs = list(
            await db.scalars(
                select(AiOperationLog).where(
                    AiOperationLog.tenant_id == tenant.tenant_id,
                    AiOperationLog.conversation_id.in_(cids),
                )
            )
        )
        if log is not None and log.conversation_id is None:
            logs.append(log)
        conversations = {
            row.conversation_id: row
            for row in await db.scalars(
                select(AiConversation).where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.conversation_id.in_(cids),
                )
            )
        }
        sources = {
            row.message_id: row
            for row in await db.scalars(
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
            logs=logs,
            conversations=conversations,
            sources=sources,
        )
        actor = await load_live_user_authority(db, tenant=tenant)
        return await project_operation(tool_call_id, db, actor, tenant, states=states)

    async def _snapshot(self, db, tx, tenant, version, filters, query=None):
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=version,
            permissions=("ai:trace:view",),
        )
        context = await load_delivery_context(
            db,
            tx,
            tenant=tenant,
            version=version,
            action="delivery.trace-list",
            agents=True,
        )
        dependencies = [("queued_at", SORT)]
        if query is not None:
            dependencies += [
                (name, EQ)
                for name in ("tool_name", "agent_code", "status")
                if getattr(query, name) is not None
            ]
            if query.queued_from is not None or query.queued_to is not None:
                dependencies.append(("queued_at", RANGE))
        await context.prove(
            db, "ai.operation_log", AiOperationLog.__table__, tuple(dependencies)
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
        names = await context.user_names(db, {r.user_id for r in rows})
        return rows, states, names, sources, context

    async def list(self, db, tx, *, tenant, actor_user_version, query):
        filters = trace_service._filters(query, tenant)
        rows, states, names, _, context = await self._snapshot(
            db, tx, tenant, actor_user_version, filters, query=query
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
            values = {"traceId": trace_id, "operationCount": len(group)}
            if (
                fields_allowed(context, group, "actor_name")
                and group[-1].user_id in names
            ):
                values.update(
                    actorId=group[-1].user_id, actorName=names[group[-1].user_id]
                )
            if (
                fields_allowed(context, group, "agent_code")
                and fields_allowed(context, group, "agent_code", GROUP)
                and all(row.agent_code in context.visible_agent_codes for row in group)
            ):
                values["agentCodes"] = sorted({row.agent_code for row in group})
            if fields_allowed(context, group, "tool_name") and fields_allowed(
                context, group, "tool_name", GROUP
            ):
                values["toolNames"] = sorted({row.tool_name for row in group})
            if fields_allowed(context, group, "status") and fields_allowed(
                context, group, "status", GROUP
            ):
                values["statuses"] = sorted({state.status for state in effective})
            if fields_allowed(context, group, "queued_at"):
                values["queuedAt"] = group[0].queued_at
            if fields_allowed(context, group, "finished_at"):
                values["finishedAt"] = (
                    max(finished)
                    if finished and not any(state.unknown for state in effective)
                    else None
                )
            records.append(TraceSummaryOut.model_validate(values))
        return DeliveryPage(
            records=records,
            total=len(ordered),
            current=query.current,
            size=query.size,
            field_policy=context.policy(
                context.record("ai.operation_log", "query", context.scopes)
            ),
        )

    async def detail(self, db, tx, *, tenant, actor_user_version, trace_id):
        rows, states, names, sources, context = await self._snapshot(
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
            record = context.record("ai.operation_log", str(row.log_id), context.scopes)
            decisions = context.decisions(record)
            values = {"log_id": row.log_id, "tool_call_id": row.tool_call_id}
            for name in (
                "tool_name",
                "execution_mode",
                "risk_level",
                "queued_at",
                "started_at",
                "hitl_wait_ms",
            ):
                if decisions[name].allowed:
                    values[name] = getattr(row, name)
            for name in ("status", "error_code", "finished_at", "duration_ms"):
                if decisions[name].allowed:
                    values[name] = getattr(state, name)
            if (
                decisions["agent_code"].allowed
                and row.agent_code in context.visible_agent_codes
            ):
                values["agent_code"] = row.agent_code
            if decisions["actor_name"].allowed and row.user_id in names:
                values.update(actor_id=row.user_id, actor_name=names[row.user_id])
            if (
                row.user_id == tenant.actor_user_id
                and decisions["target_summary"].allowed
            ):
                from app.modules.ai.service.result_projection_service import (  # noqa: PLC0415
                    result_projection_service,
                )

                actor = await load_live_user_authority(db, tenant=tenant)
                lineage = await result_projection_service.lineage_for_operation_log(
                    db, row
                )
                if await result_projection_service.authorize_result_projection(
                    db, actor, owner_user_id=row.user_id, lineage=lineage
                ):
                    values["target_summary"] = _parse_target_summary(row.target_summary)
            operations.append(TraceOperationOut.model_validate(values))
        return TraceDetailOut(
            traceId=trace_id,
            conversationId=rows[0].conversation_id,
            operations=operations,
        )


trace_candidate = TraceCandidate()
