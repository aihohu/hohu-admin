"""Direct Gateway proposals bind one new PA to the original pending log."""

from types import SimpleNamespace

from sqlalchemy import select

from app.core.authorization_plan import NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.config import settings
from app.core.id_generator import next_id
from app.core.rbac import is_super_admin
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.gateway.result import PreparedActionProposal
from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.ai.service.atomic_completion_service import DB_TOOLS, FILE_TOOLS
from app.modules.ai.service.authorization_chat_candidate import chat_candidate
from app.modules.ai.service.authorization_chat_model import recheck_model, select_model
from app.modules.ai.service.authorization_gateway_read_candidate import READ_HANDLERS
from app.modules.ai.service.operation_log_service import build_target_summary
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.preview_completion_service import (
    lock_handoff,
    validate_proposal,
)
from app.modules.ai.service.reconciliation_annotation import has_annotation
from app.modules.ai.service.result_projection_service import result_projection_service


class PendingCandidate:
    async def reference(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        log_id,
        conversation_id,
        source_id,
        trace_id,
        tool_call_id,
        tool,
        original_args_hash,
    ):
        await chat_candidate._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
        )
        log = await db.get(AiOperationLog, log_id, populate_existing=True)
        source = await db.get(AiMessage, source_id, populate_existing=True)
        expected = {
            "tenant_id": tenant.tenant_id,
            "user_id": tenant.actor_user_id,
            "conversation_id": conversation_id,
            "source_user_message_id": source_id,
            "trace_id": trace_id,
            "tool_name": tool,
            "tool_call_id": tool_call_id,
            "args_hash": original_args_hash,
            "status": "pending_confirmation",
        }
        if (
            log is None
            or any(getattr(log, key) != value for key, value in expected.items())
            or not log.confirmation_id
            or source is None
            or not source.is_active
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != conversation_id
            or source.trace_id != trace_id
            or source.role != "user"
        ):
            raise _order_error()
        return log.confirmation_id

    async def prepare(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        log_id,
        original_args_hash,
        scope_hash,
        values,
        preview_expiry=None,
        read_existing=False,
    ):
        preview = preview_expiry is not None
        if original_args_hash != compute_args_hash(values["frozen_args"]):
            raise _order_error()
        if (
            not settings.AI_MODULE_ENABLED
            or type(log_id) is not int
            or log_id <= 0
            or values["tenant"] != tenant
            or values["user_id"] != tenant.actor_user_id
            or values["interaction_flow"] != ("prepared" if preview else "direct")
            or values["requested_outcome"]
            != ("execute_if_approved" if preview else "direct")
            or (
                not preview
                and (
                    values["prepare_tool_call_id"] is not None
                    or values["prepare_tool_name"] is not None
                )
            )
            or (read_existing and not preview)
        ):
            raise _order_error()
        tool = values["execute_tool_name"]
        registered = ToolRegistry.get().find(tool)
        if (
            tool not in DB_TOOLS | FILE_TOOLS | READ_HANDLERS.keys()
            or registered is None
            or (tool == "user.import_execute") != preview
            or (
                preview
                and (
                    values["prepare_tool_name"] != "user.import_preview"
                    or not values["prepare_tool_call_id"]
                )
            )
        ):
            raise _order_error()
        meta = registered.meta
        if (
            values["agent_code"] != meta.agent
            or values["risk_level"] != meta.risk
            or values["projection_kind"]
            != (meta.projection_kind or ("scope_bound" if meta.readonly else None))
        ):
            raise _order_error()
        selection = await select_model(db, tenant, str(values["resolved_model_id"]))
        if selection.provider_id != values["resolved_provider_id"]:
            raise _order_error()
        agent_id = await db.scalar(
            select(AiAgent.agent_id).where(AiAgent.code == meta.agent)
        )
        if agent_id is None:
            raise _order_error()
        parent = ResourceKey(
            K.CONVERSATION, tenant.tenant_id, values["conversation_id"]
        )
        key = ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, next_id())
        extra = (*selection.keys, ResourceKey(K.AGENT, None, agent_id))
        if preview:
            extra += (
                ResourceKey(
                    K.IMPORT_BATCH, tenant.tenant_id, values["snapshot"]["batch_id"]
                ),
            )
        _conversation, actor = await chat_candidate._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=values["conversation_id"],
            extra=extra,
            new=() if read_existing else (NewResource(key, parent),),
        )
        await recheck_model(db, tx, tenant, selection)
        source = await db.get(
            AiMessage, values["source_user_message_id"], populate_existing=True
        )
        log = await db.get(AiOperationLog, log_id, populate_existing=True)
        if (
            source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != values["conversation_id"]
            or source.role != "user"
            or not source.is_active
            or source.trace_id != values["trace_id"]
            or log is None
            or has_annotation(log.result_summary)
        ):
            raise _order_error()
        expected = {
            "tenant_id": tenant.tenant_id,
            "user_id": tenant.actor_user_id,
            "conversation_id": values["conversation_id"],
            "source_user_message_id": source.message_id,
            "trace_id": values["trace_id"],
            "agent_code": meta.agent,
            "tool_name": tool,
            "tool_call_id": values["execute_tool_call_id"],
            "args_hash": original_args_hash,
            "readonly_snapshot": meta.readonly,
            "status": "pending_confirmation",
            "confirmation_id": values["confirmation_id"],
            "error_code": None,
            "finished_at": None,
            "started_at": None,
            "approved_by": None,
            "result_summary": None,
            "duration_ms": None,
            "hitl_wait_ms": None,
            "execution_mode": "hitl",
        }
        if not read_existing:
            expected["target_summary"] = None
        if any(getattr(log, field) != value for field, value in expected.items()):
            raise _order_error()
        if not read_existing and (
            await db.scalar(
                select(AiPreparedAction.action_id).where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.execute_tool_call_id
                    == values["execute_tool_call_id"],
                )
            )
            is not None
        ):
            raise _order_error()
        allowed = await agent_authorization_service.list_agents(
            db, actor, force_refresh=True
        )
        enabled = await get_ai_config_str_list(
            db, "ai:enabled_tools", default=[], tenant=tenant, force_refresh=True
        )
        if (
            agent_id not in {item.agent_id for item in allowed}
            or not set(meta.required_perms)
            <= agent_authorization_service.tool_permissions(actor)
            or meta.super_admin_only
            and not is_super_admin(actor)
            or not meta.default_enabled
            and tool not in enabled
            or scope_hash
            != await result_projection_service.compute_data_scope_hash(db, actor)
        ):
            raise _order_error()
        dependencies = result_projection_service.projection_dependency_message_ids(
            source
        )
        if (
            dependencies is None
            or tuple(dependencies) != tuple(values["projection_dependency_message_ids"])
            or not await result_projection_service._authorize_projection_dependencies(
                db,
                actor,
                owner_user_id=tenant.actor_user_id,
                dependency_message_ids=dependencies,
            )
        ):
            raise _order_error()
        if values["data_scope_hash"] not in {None, scope_hash}:
            raise _order_error()
        if not preview and values["snapshot"].get("argsHash") != compute_args_hash(
            values["frozen_args"]
        ):
            raise _order_error()
        if preview:
            proposal = PreparedActionProposal(
                frozen_args=values["frozen_args"],
                snapshot=values["snapshot"],
                snapshot_hash=values["snapshot_hash"],
                subject_ref=values["subject_ref"],
                presentation=values["presentation"],
                expires_at=preview_expiry,
            )
            context = SimpleNamespace(
                proposal=proposal,
                confirmation_id=values["confirmation_id"],
                prepare_tool_call_id=values["prepare_tool_call_id"],
                execute_tool_call_id=values["execute_tool_call_id"],
            )
            deps = SimpleNamespace(
                tenant=tenant,
                tenant_id=tenant.tenant_id,
                user=actor,
                conversation_id=values["conversation_id"],
                source_user_message_id=values["source_user_message_id"],
                trace_id=values["trace_id"],
                resolved_model_id=selection.model_id,
                resolved_provider_id=selection.provider_id,
                data_scope_hash=scope_hash,
                projection_dependency_message_ids=dependencies,
                guard_owner_token=values["guard_owner_token"],
                command_action=values["command_action"],
            )
            action = await lock_handoff(
                db,
                context=context,
                deps=deps,
                log_id=log_id,
                read_existing=read_existing,
                _planned=tx,
            )
            if read_existing:
                if action.expires_at != values["expires_at"]:
                    raise _order_error()
                with tx.file_io(max_reads=2, max_total_bytes=20 * 1024 * 1024):
                    await validate_proposal(
                        db,
                        proposal=proposal,
                        tenant_id=tenant.tenant_id,
                        user_id=tenant.actor_user_id,
                        _planned=tx,
                    )
                return action
        action = await prepared_action_service.create_pending(
            db, **values, _planned=(tx, key)
        )
        await tx.update_factual(
            ResourceKey(K.OPERATION_LOG, tenant.tenant_id, log_id),
            {
                "target_summary": build_target_summary(action.subject_refs),
            },
            expected={
                "status": "pending_confirmation",
                "confirmation_id": action.confirmation_id,
                "args_hash": original_args_hash,
            },
        )
        if preview:
            with tx.file_io(max_reads=2, max_total_bytes=20 * 1024 * 1024):
                await validate_proposal(
                    db,
                    proposal=proposal,
                    tenant_id=tenant.tenant_id,
                    user_id=tenant.actor_user_id,
                    _planned=tx,
                )
        return action


pending_candidate = PendingCandidate()
