"""Complete-plan execution of explicitly adapted database tools."""

import asyncio
import json
import time
from dataclasses import dataclass

from sqlalchemy import select

from app.core.auth import ensure_ai_chat_use
from app.core.authorization_plan import LockPlan, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.config import settings
from app.core.exceptions import AuthorizationException
from app.core.file_storage import ArtifactExecution
from app.core.rbac import is_super_admin
from app.modules.ai.agents.gateway.sensitive import serialize_for_llm
from app.modules.ai.agents.hitl.events import _ui_to_dict
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.service.atomic_completion_service import (
    _check_action,
    _check_log,
    _invalid,
)
from app.modules.ai.service.authorization_chat_model import select_model
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.operation_log_service import build_target_summary
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.permission_collect import collect_user_permission_codes
from app.modules.auth.service import load_live_user_authority
from app.modules.system.service.authorization_ai_tool_adapter import (
    TOOL_OPERATIONS,
    system_ai_tool_adapter,
)
from app.modules.system.service.settings_service import settings_service


@dataclass(frozen=True, slots=True)
class ExecutionDisposition:
    """No persisted result body: visibility requires a separate current projection."""

    disposition: str


class AIExecutionCandidate:
    async def reference(self, db, claim, *, tenant):
        action = await db.scalar(
            select(AiPreparedAction)
            .where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.action_id == claim.action_id,
                AiPreparedAction.user_id == tenant.actor_user_id,
            )
            .execution_options(populate_existing=True)
        )
        if action is None:
            raise _invalid("操作不存在")
        _check_action(action, claim, tenant, require_running=False)
        return action

    async def authorize(
        self, db, tx, action, *, tenant, actor_user_version, _preview=False
    ):
        tx.assert_session(db)
        current = await load_live_user_authority(db, tenant=tenant)
        if (
            not settings.AI_MODULE_ENABLED
            or type(actor_user_version) is not int
            or current.auth_version != actor_user_version
            or current.tenant.tenant_code != tenant.tenant_code
        ):
            raise AuthorizationException(
                "当前身份已经失效", error_code="AI_TOOL_PERM_DENIED"
            )
        ensure_ai_chat_use(current)
        if getattr(action, "resolved_model_id", None) is not None:
            selected = await select_model(db, tenant, str(action.resolved_model_id))
            if selected.provider_id != action.resolved_provider_id:
                raise _invalid("冻结模型的提供商绑定已变化")
        registered = ToolRegistry.get().find(action.execute_tool_name)
        preview = _preview is True and action.execute_tool_name == "user.import_preview"
        if (
            (action.execute_tool_name not in TOOL_OPERATIONS and not preview)
            or registered is None
            or registered.meta.readonly
            or (
                not registered.meta.llm_visible
                and action.execute_tool_name != "user.import_execute"
            )
            or registered.meta.interaction_flow != ("prepared" if preview else "direct")
            or action.interaction_flow
            != (
                "prepared"
                if action.execute_tool_name == "user.import_execute" or preview
                else "direct"
            )
            or action.approval_mode != "required"
            or action.dispatch_mode != "execute_after_approval"
            or action.requested_outcome
            != (
                "execute_if_approved"
                if action.execute_tool_name == "user.import_execute" or preview
                else "direct"
            )
            or registered.meta.agent != action.agent_code
        ):
            raise _invalid("工具尚未接入计划执行协议")
        meta = registered.meta
        if not set(meta.required_perms) <= collect_user_permission_codes(current):
            raise AuthorizationException(
                "工具权限已变化", error_code="AI_TOOL_PERM_DENIED"
            )
        if meta.super_admin_only and not is_super_admin(current):
            raise AuthorizationException(
                "需要超级管理员权限", error_code="AI_SUPER_ADMIN_REQUIRED"
            )
        role_ids = [r.role_id for r in current.roles if r.status == "1"]
        agent = await db.scalar(
            select(AiAgent.agent_id)
            .join(
                RoleAiAgent,
                RoleAiAgent.agent_id == AiAgent.agent_id,
            )
            .where(
                AiAgent.code == action.agent_code,
                AiAgent.enabled.is_(True),
                RoleAiAgent.tenant_id == tenant.tenant_id,
                RoleAiAgent.role_id.in_(role_ids),
                RoleAiAgent.enabled.is_(True),
            )
            .limit(1)
        )
        if agent is None:
            raise AuthorizationException(
                "Agent 授权已变化", error_code="AI_AGENT_FORBIDDEN"
            )
        raw = await settings_service.get_value(
            db, "ai:enabled_tools", "[]", tenant=tenant
        )
        try:
            extra = json.loads(raw)
        except (TypeError, ValueError):
            extra = []
        if not isinstance(extra, list) or any(type(n) is not str for n in extra):
            extra = []
        if not meta.default_enabled and meta.name not in extra:
            raise AuthorizationException("工具未启用", error_code="AI_TOOL_PERM_DENIED")
        scope_hash = await result_projection_service.compute_data_scope_hash(
            db, current
        )
        if action.data_scope_hash is not None and scope_hash != action.data_scope_hash:
            raise AuthorizationException(
                "数据权限已变化", error_code="AI_DATA_SCOPE_VIOLATION"
            )
        dependencies = result_projection_service.projection_dependency_message_ids(
            action
        )
        if (
            dependencies is None
            or not await result_projection_service._authorize_projection_dependencies(
                db,
                current,
                owner_user_id=tenant.actor_user_id,
                dependency_message_ids=dependencies,
            )
        ):
            raise AuthorizationException(
                "历史上下文权限已变化", error_code="AI_RESULT_PROJECTION_FORBIDDEN"
            )
        return meta, scope_hash

    async def execute(self, db, tx, command, claim, *, tenant, actor_user_version):
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        started = time.monotonic()
        action = await self.reference(db, claim, tenant=tenant)
        _check_action(action, claim, tenant, require_running=True)
        system_ai_tool_adapter.validate_binding(
            command,
            tool=action.execute_tool_name,
            args=action.frozen_args,
            snapshot=(
                {k: v for k, v in action.snapshot.items() if k != "_authorization"}
                if action.execute_tool_name == "user.import_execute"
                else action.snapshot.get("business")
            ),
            tenant=tenant,
            actor_user_version=actor_user_version,
        )
        if action.execute_tool_name in {
            "user.export",
            "user.import_execute",
        } and command.command.execution != ArtifactExecution(
            claim.tenant_id, str(claim.action_id), claim.execution_owner
        ):
            raise _invalid("FILE 执行归属已变化")
        await self.authorize(
            db, tx, action, tenant=tenant, actor_user_version=actor_user_version
        )
        # Start the smaller execution budget before domain planning/locking.
        raw_timeout = await settings_service.get_value(
            db, "ai:limit:tool_timeout_sec", "10", tenant=tenant
        )
        try:
            timeout = min(60, int(raw_timeout))
        except (ValueError, TypeError):
            raise _invalid("工具执行预算无效") from None
        # database_now requires a complete locked plan. The initial budget applies
        # L3; lease is rechecked with the database clock immediately after locking.
        if timeout <= 0:
            raise _invalid("工具执行预算无效")
        async with asyncio.timeout_at(started + timeout):
            inventory = await ai_context_candidate.discover(
                db,
                tx,
                tenant_id=tenant.tenant_id,
                conversation_ids=(claim.conversation_id,),
            )
            work = await system_ai_tool_adapter.plan(db, tx, command)
            await tx.lock(
                LockPlan(
                    existing=(*inventory.keys, *work.locks.existing), new=work.locks.new
                )
            )
            await ai_context_candidate.recheck(db, tx, inventory)
            action = await self.reference(db, claim, tenant=tenant)
            _check_action(action, claim, tenant, require_running=True)
            conversation = await db.scalar(
                select(AiConversation).where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.conversation_id == claim.conversation_id,
                )
            )
            source = await db.scalar(
                select(AiMessage).where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.message_id == claim.source_user_message_id,
                )
            )
            log = await db.scalar(
                select(AiOperationLog).where(
                    AiOperationLog.tenant_id == tenant.tenant_id,
                    AiOperationLog.log_id == claim.log_id,
                )
            )
            if (
                conversation is None
                or source is None
                or log is None
                or conversation.user_id != tenant.actor_user_id
                or conversation.deleted_at is not None
                or source.conversation_id != claim.conversation_id
                or source.role != "user"
                or source.trace_id != claim.trace_id
                or not source.is_active
            ):
                raise _invalid("操作上下文已变化")
            _check_log(action, log, "running")
            tx.assert_held(
                ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, claim.action_id),
                ResourceKey(K.OPERATION_LOG, tenant.tenant_id, claim.log_id),
            )
            meta, _ = await self.authorize(
                db, tx, action, tenant=tenant, actor_user_version=actor_user_version
            )
            now = await tx.database_now()
            lease = action.execution_lease_expires_at
            if (
                lease is None
                or lease.tzinfo is None
                or (lease - now).total_seconds() <= 10
            ):
                raise _invalid("执行认领租约预算不足")
            async with asyncio.timeout((lease - now).total_seconds() - 10):
                result = await system_ai_tool_adapter.apply(db, tx, command, work)
                meta, scope_hash = await self.authorize(
                    db, tx, action, tenant=tenant, actor_user_version=actor_user_version
                )
                result.data = serialize_for_llm(meta.sensitive_output, result.data)
                await self._complete(
                    db,
                    tx,
                    action,
                    log,
                    claim,
                    result,
                    tenant,
                    scope_hash,
                    int((time.monotonic() - started) * 1000),
                )
        return ExecutionDisposition("succeeded")

    async def _complete(
        self, db, tx, action, log, claim, result, tenant, scope_hash, duration
    ):
        tx.assert_session(db)
        if (
            not result.ok
            or result.prepared_action is not None
            or result.projection is None
        ):
            raise _invalid("执行结果缺少来源证明")
        ui = _ui_to_dict(result.ui)
        json.dumps({"data": result.data, "ui": ui}, allow_nan=False)
        lineage = result_projection_service.freeze_lineage(
            tenant=tenant,
            agent_code=claim.agent_code,
            tool_codes=action.tool_codes or [claim.execute_tool_name],
            subject_refs=result.projection.subject_refs,
            data_scope_hash=scope_hash if result.projection.scope_bound else None,
            projection_dependency_message_ids=action.projection_dependency_message_ids
            or (),
        )
        summary = build_target_summary(lineage.subject_refs)
        if lineage.subject_refs and summary is None:
            raise _invalid("结果缺少安全对象引用")
        now = await tx.database_now()
        await tx.update_factual(
            ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, claim.action_id),
            {
                "status": "succeeded",
                "row_version": claim.row_version + 1,
                "execution_owner": None,
                "execution_lease_expires_at": None,
                "finished_at": now,
                "error_code": None,
                "duration_ms": duration,
                "result_data": result.data,
                "result_ui": ui,
                "subject_refs": list(lineage.subject_refs),
                "subject_refs_hash": lineage.subject_refs_hash,
                "tool_codes": list(lineage.tool_codes),
                "resolver_version": lineage.resolver_version,
                "data_scope_hash": lineage.data_scope_hash,
            },
            expected={
                "status": "running",
                "row_version": claim.row_version,
                "execution_owner": claim.execution_owner,
            },
        )
        await tx.update_factual(
            ResourceKey(K.OPERATION_LOG, tenant.tenant_id, claim.log_id),
            {
                "status": "success",
                "finished_at": max(
                    now.replace(tzinfo=None), log.queued_at, log.started_at
                ),
                "duration_ms": duration,
                "error_code": None,
                "result_summary": "prepared action succeeded",
                "target_summary": summary,
            },
            expected={"status": "running", "finished_at": None},
        )


ai_execution_candidate = AIExecutionCandidate()
