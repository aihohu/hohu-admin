"""Fixed chat fact writers and coherent current-authority projections."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select

from app.core.auth import ensure_ai_chat_use
from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.base_response import PageResult
from app.core.exceptions import BusinessException, NotFoundException
from app.core.id_generator import next_id
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.gateway.redact import redact_secrets
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.conversation import ConversationOut
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    FILE_TOOLS,
    is_registered_readonly_action,
)
from app.modules.ai.service.authorization_chat_model import recheck_model
from app.modules.ai.service.authorization_completion_projection import (
    project_completions,
)
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.authorization_lifecycle_candidate import lifecycle_candidate
from app.modules.ai.service.chat_run_service import (
    chat_run_finalizer,
    enforce_grounded_management_write_claim,
    enforce_import_error_output_redaction,
)
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.preflight_completion_service import has_preflight_marker
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.reconciliation_annotation import (
    has_annotation,
    reconciliation_notice,
)
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import (
    load_background_projection_authority,
    load_live_user_authority,
)
from app.modules.system.models.user import User
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.utils.pagination import build_filters


@dataclass(frozen=True, slots=True)
class SourceReference:
    message_id: int
    trace_id: str


class ChatCandidate:
    async def list_conversations(self, db, tx, *, tenant, actor_user_version, query):
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("ai:chat:use",),
        )
        owned = (
            select(AiConversation.conversation_id)
            .where(
                AiConversation.tenant_id == tenant.tenant_id,
                AiConversation.user_id == tenant.actor_user_id,
                AiConversation.deleted_at.is_(None),
            )
            .order_by(AiConversation.conversation_id)
        )
        ids = tuple(await db.scalars(owned))
        await tx.lock(
            LockPlan(
                existing=(
                    *identity.keys,
                    *(
                        ResourceKey(K.CONVERSATION, tenant.tenant_id, cid)
                        for cid in ids
                    ),
                )
            )
        )
        await assert_identity(db, identity)
        if tuple(await db.scalars(owned)) != ids:
            raise _order_error()
        filters = build_filters(
            AiConversation,
            {"title": ("title", "contains"), "status": "status"},
            **query.model_dump(exclude_unset=True),
        )
        filters.extend(
            (
                AiConversation.tenant_id == tenant.tenant_id,
                AiConversation.conversation_id.in_(ids),
            )
        )
        total = await db.scalar(
            select(func.pg_catalog.count(AiConversation.conversation_id)).where(
                *filters
            )
        )
        rows = await db.scalars(
            select(AiConversation)
            .where(*filters)
            .order_by(
                AiConversation.update_time.desc(), AiConversation.conversation_id.desc()
            )
            .offset((query.current - 1) * query.size)
            .limit(query.size)
            .execution_options(populate_existing=True)
        )
        return PageResult(
            records=[ConversationOut.model_validate(row) for row in rows],
            total=total,
            current=query.current,
            size=query.size,
        )

    async def conversation(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        data,
        selection,
        conversation_id=None,
    ):
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("ai:chat:use",),
        )
        inventory = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            conversation_ids=() if conversation_id is None else (conversation_id,),
        )
        key = ResourceKey(
            K.CONVERSATION, tenant.tenant_id, conversation_id or next_id()
        )
        await tx.lock(
            LockPlan(
                existing=tuple(
                    set(identity.keys)
                    | set(inventory.keys)
                    | set(selection.keys if selection else ())
                ),
                new=(NewResource(key),) if conversation_id is None else (),
            )
        )
        await ai_context_candidate.recheck(db, tx, inventory)
        await assert_identity(db, identity)
        actor = await load_live_user_authority(db, tenant=tenant)
        ensure_ai_chat_use(actor)
        if selection is not None:
            await recheck_model(db, tx, tenant, selection)
            data = dict(data, model_name=str(selection.model_id))
        if conversation_id is None:
            if selection is None:
                raise _order_error()
            await tx.insert_reserved(key, dict(data, user_id=tenant.actor_user_id))
        else:
            row = await db.get(AiConversation, conversation_id, populate_existing=True)
            if (
                row is None
                or row.tenant_id != tenant.tenant_id
                or row.user_id != tenant.actor_user_id
                or row.deleted_at is not None
            ):
                raise NotFoundException(
                    "AI会话", error_code="AI_CONVERSATION_NOT_FOUND"
                )
            if data:
                await tx.update_factual(
                    key, data, expected={name: getattr(row, name) for name in data}
                )
        return key.identity[0]

    async def _lock(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        conversation_id,
        new=(),
        background=False,
    ):
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("ai:chat:use",),
        )
        inventory = await ai_context_candidate.discover(
            db, tx, tenant_id=tenant.tenant_id, conversation_ids=(conversation_id,)
        )
        await tx.lock(LockPlan(existing=(*identity.keys, *inventory.keys), new=new))
        await ai_context_candidate.recheck(db, tx, inventory)
        await assert_identity(db, identity)
        actor = (
            await load_background_projection_authority(db, tenant=tenant, tx=tx)
            if background and tenant.source == "platform_control"
            else await load_live_user_authority(db, tenant=tenant)
        )
        ensure_ai_chat_use(actor)
        conversation = await db.get(
            AiConversation, conversation_id, populate_existing=True
        )
        if (
            conversation is None
            or conversation.tenant_id != tenant.tenant_id
            or conversation.user_id != tenant.actor_user_id
            or conversation.deleted_at is not None
        ):
            raise NotFoundException("AI会话", error_code="AI_CONVERSATION_NOT_FOUND")
        return conversation, actor

    async def source(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        conversation_id,
        content,
        supersedes_message_id=None,
    ):
        if (
            type(content) is not str
            or not content
            or len(content.encode()) > 256 * 1024
        ):
            raise _order_error()
        reference = SourceReference(next_id(), uuid4().hex)
        parent = ResourceKey(K.CONVERSATION, tenant.tenant_id, conversation_id)
        key = ResourceKey(K.SOURCE_MESSAGE, tenant.tenant_id, reference.message_id)
        conversation, actor = await self._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
            new=(NewResource(key, parent),),
        )
        await self._settle_context(
            db,
            tx,
            tenant,
            conversation,
            cancel_pending=supersedes_message_id is not None,
        )
        if supersedes_message_id is not None:
            await self._invalidate_source(
                db, tx, tenant, conversation_id, supersedes_message_id
            )
        dependencies = (
            await result_projection_service.collect_message_projection_dependencies(
                db, conversation_id=conversation_id, tenant=tenant
            )
        )
        if not await result_projection_service._authorize_projection_dependencies(
            db,
            actor,
            owner_user_id=tenant.actor_user_id,
            dependency_message_ids=tuple(dependencies),
        ):
            raise _order_error()
        await tx.insert_reserved(
            key,
            {
                "conversation_id": conversation_id,
                "role": "user",
                "content": content,
                "trace_id": reference.trace_id,
                "is_active": True,
                "supersedes_message_id": supersedes_message_id,
                "projection_dependency_message_ids": [str(v) for v in dependencies],
            },
        )
        return reference

    async def _settle_context(self, db, tx, tenant, conversation, *, cancel_pending):
        def unresolved():
            return BusinessException(
                409,
                "会话操作正在核对，请稍后刷新确认状态",
                error_code="AI_ACTION_RUNNING",
            )

        actions, logs = await self._facts(db, tenant, conversation.conversation_id)
        sources = {
            row.message_id: row
            for row in await db.scalars(
                select(AiMessage).where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.conversation_id == conversation.conversation_id,
                    AiMessage.role == "user",
                )
            )
        }
        states = await project_completions(
            db,
            tenant=tenant,
            actions=actions,
            logs=list(logs.values()),
            conversations={conversation.conversation_id: conversation},
            sources=sources,
        )

        def readonly_terminal(log):
            if log is None or log.tool_name in DB_TOOLS | FILE_TOOLS:
                return False
            tool = ToolRegistry.get().find(log.tool_name)
            source = sources.get(log.source_user_message_id)
            return bool(
                tool is not None
                and tool.meta.readonly
                and tool.meta.agent == log.agent_code
                and log.readonly_snapshot is True
                and log.tenant_id == tenant.tenant_id
                and log.user_id == conversation.user_id
                and log.conversation_id == conversation.conversation_id
                and source is not None
                and source.trace_id == log.trace_id
                and source.conversation_id == log.conversation_id
                and log.status in {"success", "failed", "expired", "rejected"}
                and log.finished_at is not None
                and log.queued_at is not None
                and log.finished_at >= log.queued_at
                and (
                    log.started_at is None
                    or log.queued_at <= log.started_at <= log.finished_at
                )
                and (
                    log.status != "success"
                    or (log.started_at is not None and log.error_code is None)
                )
                and (log.status != "failed" or bool(log.error_code))
                and type(log.args_hash) is str
                and len(log.args_hash) == 64
                and all(char in "0123456789abcdef" for char in log.args_hash)
                and not has_annotation(log.result_summary)
            )

        for action in actions:
            if cancel_pending and action.status == "pending_confirmation":
                if (
                    compute_args_hash(action.frozen_args) != action.args_hash
                    or compute_args_hash(action.snapshot) != action.snapshot_hash
                ):
                    raise unresolved()
                log = logs.get(action.execute_tool_call_id)
                if log is None:
                    raise unresolved()
                await lifecycle_candidate._finish_decision(
                    tx, tenant, action, log, "cancel"
                )
            elif readonly_terminal(logs.get(action.execute_tool_call_id)):
                try:
                    lifecycle_candidate.verify_readonly_terminal(
                        action, logs.get(action.execute_tool_call_id)
                    )
                except BusinessException as exc:
                    raise unresolved() from exc
                continue
            elif (
                action.execute_tool_call_id not in states
                or not states[action.execute_tool_call_id].verified
            ):
                raise unresolved()
        bound = {row.execute_tool_call_id for row in actions}
        if any(
            call not in bound
            and not readonly_terminal(logs[call])
            and (call not in states or not states[call].verified)
            for call in logs
        ):
            raise unresolved()

    async def _invalidate_source(self, db, tx, tenant, conversation_id, source_id):
        rows = list(
            await db.scalars(
                select(AiMessage)
                .where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.conversation_id == conversation_id,
                )
                .execution_options(populate_existing=True)
            )
        )
        source = next(
            (
                r
                for r in rows
                if r.message_id == source_id and r.role == "user" and r.is_active
            ),
            None,
        )
        if source is None:
            raise _order_error()
        affected = {source_id}
        while True:
            following = {
                r.message_id
                for r in rows
                if r.parent_message_id in affected
                or set(
                    result_projection_service.projection_dependency_message_ids(r) or ()
                )
                & affected
            }
            if following <= affected:
                break
            affected.update(following)
        for row in rows:
            if row.message_id in affected and row.is_active:
                kind = K.SOURCE_MESSAGE if row.role == "user" else K.ASSISTANT_MESSAGE
                await tx.update_factual(
                    ResourceKey(kind, tenant.tenant_id, row.message_id),
                    {"is_active": False},
                    expected={"is_active": True},
                )

    async def invalidate(
        self, db, tx, *, tenant, actor_user_version, conversation_id, source_id=None
    ):
        conversation, _ = await self._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
        )
        await self._settle_context(db, tx, tenant, conversation, cancel_pending=True)
        if source_id is not None:
            await self._invalidate_source(db, tx, tenant, conversation_id, source_id)
        else:
            await tx.update_factual(
                ResourceKey(K.CONVERSATION, tenant.tenant_id, conversation_id),
                {"deleted_at": await tx.database_now()},
                expected={"deleted_at": None},
            )

    async def recover(self, db, tx, *, tenant, confirmation_id):
        reference = await lifecycle_candidate.reference(
            db, confirmation_id, tenant=tenant
        )
        if reference is None:
            raise _order_error()
        version = await db.scalar(
            select(User.auth_version).where(
                User.tenant_id == tenant.tenant_id, User.user_id == tenant.actor_user_id
            )
        )
        return await self.finalize(
            db,
            tx,
            tenant=tenant,
            actor_user_version=version,
            conversation_id=reference.conversation_id,
            source_id=reference.source_user_message_id,
            trace_id=reference.trace_id,
            content="",
            tool_calls=[],
            agent_code=reference.agent_code,
            recovery_confirmation_id=confirmation_id,
        )

    async def recover_orphan(self, db, tx, *, tenant, confirmation_id):
        reference = (
            await db.execute(
                select(AiOperationLog).where(
                    AiOperationLog.tenant_id == tenant.tenant_id,
                    AiOperationLog.user_id == tenant.actor_user_id,
                    AiOperationLog.confirmation_id == confirmation_id,
                )
            )
        ).scalar_one_or_none()
        if reference is None:
            raise _order_error()
        version = await db.scalar(
            select(User.auth_version).where(
                User.tenant_id == tenant.tenant_id, User.user_id == tenant.actor_user_id
            )
        )
        return await self.finalize(
            db,
            tx,
            tenant=tenant,
            actor_user_version=version,
            conversation_id=reference.conversation_id,
            source_id=reference.source_user_message_id,
            trace_id=reference.trace_id,
            content="",
            tool_calls=[],
            agent_code=reference.agent_code,
            recovery_log=(reference.log_id, confirmation_id),
        )

    async def finalize(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        conversation_id,
        source_id,
        trace_id,
        content,
        tool_calls,
        agent_code,
        recovery_confirmation_id=None,
        recovery_log=None,
    ):
        if (
            type(content) is not str
            or len(content.encode()) > 256 * 1024
            or type(tool_calls) is not list
            or len(tool_calls) > 256
        ):
            raise _order_error()
        # Discover the unique assistant before reserving its ID. C serializes inserts.
        existing = await db.scalar(
            select(AiMessage.message_id).where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.conversation_id == conversation_id,
                AiMessage.trace_id == trace_id,
                AiMessage.role == "assistant",
            )
        )
        key = ResourceKey(K.ASSISTANT_MESSAGE, tenant.tenant_id, existing or next_id())
        parent = ResourceKey(K.CONVERSATION, tenant.tenant_id, conversation_id)
        conversation, actor = await self._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
            new=() if existing else (NewResource(key, parent),),
            background=recovery_confirmation_id is not None or recovery_log is not None,
        )
        source = await db.get(AiMessage, source_id, populate_existing=True)
        if (
            source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != conversation_id
            or source.role != "user"
            or source.trace_id != trace_id
            or not source.is_active
        ):
            raise _order_error()
        actions, logs = await self._facts(
            db, tenant, conversation_id, source_id=source_id, trace_id=trace_id
        )
        if recovery_confirmation_id is not None:
            action = next(
                (a for a in actions if a.confirmation_id == recovery_confirmation_id),
                None,
            )
            if action is None or action.status not in {
                "expired",
                "failed",
                "succeeded",
                "rejected",
            }:
                raise _order_error()
            if content or tool_calls or action.agent_code != agent_code:
                raise _order_error()
            tool_calls = [{"tool_call_id": action.execute_tool_call_id}]
        protected, unknown, lineages = await chat_run_finalizer._protected_cards(
            db,
            conversation=conversation,
            source=source,
            actions=actions,
            logs=logs,
            tenant=tenant,
            actor=actor,
        )
        if recovery_confirmation_id is not None and is_registered_readonly_action(
            action
        ):
            lineage, card = await self._readonly_recovery_card(
                db, tenant, actor, action, logs
            )
            protected[action.execute_tool_call_id] = card
            if lineage is not None:
                lineages.append(lineage)
        if recovery_log is not None:
            log = next((v for v in logs.values() if v.log_id == recovery_log[0]), None)
            if (
                content
                or tool_calls
                or recovery_confirmation_id is not None
                or log is None
                or log.confirmation_id != recovery_log[1]
                or log.agent_code != agent_code
            ):
                raise _order_error()
            if (
                await db.scalar(
                    select(AiPreparedAction.action_id).where(
                        AiPreparedAction.tenant_id == tenant.tenant_id,
                        AiPreparedAction.confirmation_id == recovery_log[1],
                    )
                )
                is not None
            ):
                raise _order_error()
            registered = ToolRegistry.get().find(log.tool_name)
            if (
                registered is None
                or registered.meta.readonly is not True
                or registered.meta.agent != log.agent_code
                or log.tool_name in DB_TOOLS | FILE_TOOLS
                or log.readonly_snapshot is not True
                or log.execution_mode != "hitl"
                or log.status not in {"expired", "failed", "rejected"}
                or log.finished_at is None
                or log.user_id != tenant.actor_user_id
                or has_annotation(log.result_summary)
                or has_preflight_marker(log.result_summary)
            ):
                raise _order_error()
            lineage = result_projection_service.freeze_lineage(
                tenant=tenant,
                agent_code=log.agent_code,
                tool_codes=[log.tool_name],
                subject_refs=[],
                data_scope_hash=await result_projection_service.compute_data_scope_hash(
                    db, actor
                ),
            )
            if not await result_projection_service.authorize_result_projection(
                db, actor, owner_user_id=tenant.actor_user_id, lineage=lineage
            ):
                raise _order_error()
            protected[log.tool_call_id] = {
                "tool": log.tool_name,
                "tool_call_id": log.tool_call_id,
                "ok": False,
                "error_code": log.error_code
                or (
                    "USER_REJECTED" if log.status == "rejected" else "AI_INTERNAL_ERROR"
                ),
                "error_msg": "确认已过期，请重新发起"
                if log.status == "expired"
                else "操作未完成，请重新发起",
                "result": None,
                "ui": None,
                "args": {},
                "duration_ms": log.duration_ms or 0,
            }
            tool_calls = [{"tool_call_id": log.tool_call_id}]
            lineages.append(lineage)
        # Unknown user-supplied write cards are never accepted as a result source.
        known = set(logs)
        if any(
            type(card) is not dict or card.get("tool_call_id") not in known
            for card in tool_calls
        ):
            raise _order_error()
        cards = chat_run_finalizer._replace_protected_cards(tool_calls, protected)
        content, _ = enforce_grounded_management_write_claim(
            content, agent_code=agent_code, tool_calls=cards
        )
        content, _ = enforce_import_error_output_redaction(content, tool_calls=cards)
        if unknown:
            content = reconciliation_notice(
                "",
                [
                    key
                    for key, card in protected.items()
                    if card is not None and "ok" not in card
                ],
            )
        lineage = None
        for value in lineages:
            if lineage is not None and (
                lineage.agent_code != value.agent_code
                or lineage.data_scope_hash != value.data_scope_hash
            ):
                raise _order_error()
            lineage = result_projection_service.freeze_lineage(
                tenant=tenant,
                agent_code=value.agent_code,
                tool_codes=(
                    *(lineage.tool_codes if lineage else ()),
                    *value.tool_codes,
                ),
                subject_refs=(
                    *(lineage.subject_refs if lineage else ()),
                    *value.subject_refs,
                ),
                data_scope_hash=value.data_scope_hash,
                projection_dependency_message_ids=(
                    *(lineage.projection_dependency_message_ids if lineage else ()),
                    *value.projection_dependency_message_ids,
                ),
            )
        dependencies = result_projection_service.projection_dependency_message_ids(
            source
        )
        if (
            dependencies is None
            or not await result_projection_service._authorize_projection_dependencies(
                db,
                actor,
                owner_user_id=tenant.actor_user_id,
                dependency_message_ids=dependencies,
            )
        ):
            raise _order_error()
        hidden = any(card is None for card in protected.values())
        if hidden:
            content = ""
        if lineage is None and agent_code is not None and not hidden:
            lineage = result_projection_service.freeze_lineage(
                tenant=tenant,
                agent_code=agent_code,
                tool_codes=[],
                subject_refs=[],
                data_scope_hash=await result_projection_service.compute_data_scope_hash(
                    db, actor
                ),
                projection_dependency_message_ids=dependencies,
            )
            if not await result_projection_service.authorize_result_projection(
                db, actor, owner_user_id=tenant.actor_user_id, lineage=lineage
            ):
                raise _order_error()
        elif lineage is not None:
            lineage = result_projection_service.freeze_lineage(
                tenant=tenant,
                agent_code=lineage.agent_code,
                tool_codes=lineage.tool_codes,
                subject_refs=lineage.subject_refs,
                data_scope_hash=lineage.data_scope_hash,
                projection_dependency_message_ids=(
                    *lineage.projection_dependency_message_ids,
                    *dependencies,
                ),
            )
        message = (
            await db.get(AiMessage, existing, populate_existing=True)
            if existing
            else None
        )
        if message is not None and (
            message.parent_message_id != source_id or message.agent_code != agent_code
        ):
            raise _order_error()
        if message is not None:
            prior_dependencies = (
                result_projection_service.projection_dependency_message_ids(message)
            )
            if prior_dependencies is None:
                raise _order_error()
            dependencies = tuple(sorted(set(dependencies) | set(prior_dependencies)))
            if not await result_projection_service._authorize_projection_dependencies(
                db,
                actor,
                owner_user_id=tenant.actor_user_id,
                dependency_message_ids=dependencies,
            ):
                raise _order_error()
            prior = result_projection_service.lineage_from_record(message)
            if prior is not None:
                if (
                    lineage is None
                    or prior.agent_code != lineage.agent_code
                    or prior.data_scope_hash != lineage.data_scope_hash
                ):
                    raise _order_error()
                lineage = result_projection_service.freeze_lineage(
                    tenant=tenant,
                    agent_code=lineage.agent_code,
                    tool_codes=(*prior.tool_codes, *lineage.tool_codes),
                    subject_refs=(*prior.subject_refs, *lineage.subject_refs),
                    data_scope_hash=lineage.data_scope_hash,
                    projection_dependency_message_ids=(
                        *prior.projection_dependency_message_ids,
                        *lineage.projection_dependency_message_ids,
                    ),
                )
        changes = {
            "projection_dependency_message_ids": [str(v) for v in dependencies],
            "content": content
            if unknown or hidden or message is None or not message.content
            else message.content,
            "tool_calls": chat_run_finalizer._merge_tool_calls(
                chat_run_finalizer._replace_protected_cards(
                    message.tool_calls, protected
                )
                if message
                else None,
                cards,
            ),
        }
        if lineage is not None:
            changes.update(
                tool_codes=list(lineage.tool_codes),
                subject_refs=list(lineage.subject_refs),
                subject_refs_hash=lineage.subject_refs_hash,
                data_scope_hash=lineage.data_scope_hash,
                resolver_version=lineage.resolver_version,
                projection_dependency_message_ids=[
                    str(v) for v in lineage.projection_dependency_message_ids
                ],
            )
        if message is None:
            await tx.insert_reserved(
                key,
                dict(
                    conversation_id=conversation_id,
                    parent_message_id=source_id,
                    role="assistant",
                    trace_id=trace_id,
                    agent_code=agent_code,
                    is_active=True,
                    **changes,
                ),
            )
        else:
            await tx.update_factual(
                key,
                changes,
                expected={"content": message.content, "tool_calls": message.tool_calls},
            )
        return key.identity[0]

    async def _readonly_recovery_card(self, db, tenant, actor, action, logs):
        if action.status == "succeeded":
            # Legacy readonly summaries are not reconstructible result payloads.
            raise _order_error()
        log = logs.get(action.execute_tool_call_id)
        lifecycle_candidate.verify_readonly_terminal(action, log)
        lineage = result_projection_service.lineage_from_record(action)
        if (
            lineage is None
            or action.execute_tool_name not in lineage.tool_codes
            or not await result_projection_service.authorize_result_projection(
                db, actor, owner_user_id=tenant.actor_user_id, lineage=lineage
            )
        ):
            return None, None
        return lineage, {
            "tool": action.execute_tool_name,
            "tool_call_id": action.execute_tool_call_id,
            "ok": False,
            "error_code": action.error_code,
            "error_msg": "确认已过期，请重新发起"
            if action.status == "expired"
            else "执行已中断，请重新发起",
            "result": None,
            "ui": None,
            "duration_ms": action.duration_ms or 0,
            "trace_id": action.trace_id,
            "args": {},
        }

    async def _facts(
        self, db, tenant, conversation_id, *, source_id=None, trace_id=None
    ):
        def filters(model):
            result = [
                model.tenant_id == tenant.tenant_id,
                model.conversation_id == conversation_id,
            ]
            if source_id is not None:
                result.append(model.source_user_message_id == source_id)
            if trace_id is not None:
                result.append(model.trace_id == trace_id)
            return result

        actions = list(
            await db.scalars(
                select(AiPreparedAction)
                .where(*filters(AiPreparedAction))
                .order_by(AiPreparedAction.action_id)
                .execution_options(populate_existing=True)
            )
        )
        logs = {
            row.tool_call_id: row
            for row in await db.scalars(
                select(AiOperationLog)
                .where(*filters(AiOperationLog))
                .order_by(AiOperationLog.log_id)
                .execution_options(populate_existing=True)
            )
        }
        return actions, logs

    async def messages(
        self, db, tx, *, tenant, actor_user_version, conversation_id, detail=False
    ):
        conversation, actor = await self._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
        )
        messages = list(
            await db.scalars(
                select(AiMessage)
                .where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.conversation_id == conversation_id,
                )
                .order_by(AiMessage.message_id)
                .execution_options(populate_existing=True)
            )
        )
        actions, logs = await self._facts(db, tenant, conversation_id)
        states = await project_completions(
            db,
            tenant=tenant,
            actions=actions,
            logs=list(logs.values()),
            conversations={conversation_id: conversation},
            sources={row.message_id: row for row in messages if row.role == "user"},
        )
        projected = await conversation_service.project_locked_messages(
            db,
            current_user=actor,
            messages=[message for message in messages if message.is_active],
            completion_states=states,
        )
        for output in projected:
            if getattr(output, "content", None):
                output.content = redact_secrets(output.content)
        if detail:
            pending = await prepared_action_service.list_pending_for_conversation(
                db,
                conversation_id=conversation_id,
                user_id=tenant.actor_user_id,
                tenant=tenant,
            )
            return {
                "conversation": ConversationOut.model_validate(conversation),
                "messages": projected,
                "pendingActions": [
                    await prepared_action_service.project_pending_out(
                        db, action=action, current_user=actor
                    )
                    for action in pending
                ],
            }
        return projected


chat_candidate = ChatCandidate()
