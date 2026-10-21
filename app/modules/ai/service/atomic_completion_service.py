"""Locked completion facts for Gateway-owned prepared-action transactions.

This service never commits or infers rollback from missing business records.
The Gateway must confirm rollback before calling ``complete_failed`` in a new
transaction. File-side-effect disposition is deliberately not inferred here.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.hitl.constants import PreparedActionStatus
from app.modules.ai.agents.hitl.events import _ui_to_dict
from app.modules.ai.core.context import ChatDeps
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.operation_log_service import build_target_summary
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import load_live_user_authority

DB_TOOLS = frozenset(
    {
        "job.update_cron",
        "dept.create",
        "dept.update",
        "dept.move",
        "role.create",
        "role.update",
        "role.update_menus",
        "role.update_agents",
        "user.update_dept",
        "user.update_roles",
        "user.create",
        "user.reset_password",
        "user.batch_delete",
        "user.update",
    }
)
FILE_TOOLS = frozenset({"user.import_execute", "user.export", "user.import_preview"})
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


def _invalid(message: str) -> BusinessRuleException:
    return BusinessRuleException(
        message, error_code="AI_PREPARED_ACTION_BINDING_INVALID"
    )


def _owner_prefix(tool_name: str) -> str | None:
    if tool_name in DB_TOOLS:
        return "v0162-db:"
    if tool_name in FILE_TOOLS:
        return "v0162-file:"
    return None


def is_protocol_owner(owner: str | None, tool_name: str) -> bool:
    """Accept only a canonical UUID in the explicitly classified tool protocol."""
    prefix = _owner_prefix(tool_name)
    if prefix is None or not isinstance(owner, str) or not owner.startswith(prefix):
        return False
    value = owner[len(prefix) :]
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def has_protocol_marker(owner: str | None) -> bool:
    """Reserve malformed protocol markers too; they never prove legacy failure."""
    return isinstance(owner, str) and owner.startswith("v0162-")


def new_execution_owner(tool_name: str) -> str:
    """Create a server-only owner; unclassified tools cannot fall back to DB."""
    prefix = _owner_prefix(tool_name)
    if prefix is None:
        raise _invalid("工具尚未声明原子完成协议")
    return f"{prefix}{uuid4()}"


@dataclass(frozen=True, slots=True)
class ClaimBinding:
    """Immutable scalar identity, never a mutable ORM execution credential."""

    tenant_id: int
    action_id: int
    confirmation_id: str
    execute_tool_call_id: str
    execute_tool_name: str
    actor_user_id: int
    conversation_id: int
    source_user_message_id: int
    trace_id: str
    agent_code: str
    args_hash: str
    snapshot_hash: str
    execution_owner: str
    row_version: int
    log_id: int

    @property
    def tool_name(self) -> str:
        return self.execute_tool_name

    @property
    def tool_call_id(self) -> str:
        return self.execute_tool_call_id

    @property
    def user_id(self) -> int:
        return self.actor_user_id

    @classmethod
    def from_action(cls, action: AiPreparedAction, log_id: int) -> "ClaimBinding":
        if action.status != "running" or not is_protocol_owner(
            action.execution_owner, action.execute_tool_name
        ):
            raise _invalid("执行凭证必须来自已认领的新协议操作")
        for value in (
            action.action_id,
            action.user_id,
            action.conversation_id,
            action.source_user_message_id,
            action.row_version,
            log_id,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise _invalid("执行凭证缺少稳定的正整数绑定")
        for value in (
            action.confirmation_id,
            action.execute_tool_call_id,
            action.trace_id,
            action.agent_code,
        ):
            if not isinstance(value, str) or not value:
                raise _invalid("执行凭证缺少必需绑定")
        if not all(
            isinstance(value, str) and _HASH_RE.fullmatch(value)
            for value in (action.args_hash, action.snapshot_hash)
        ):
            raise _invalid("执行凭证摘要无效")
        return cls(
            tenant_id=action.tenant_id,
            action_id=action.action_id,
            confirmation_id=action.confirmation_id,
            execute_tool_call_id=action.execute_tool_call_id,
            execute_tool_name=action.execute_tool_name,
            actor_user_id=action.user_id,
            conversation_id=action.conversation_id,
            source_user_message_id=action.source_user_message_id,
            trace_id=action.trace_id,
            agent_code=action.agent_code,
            args_hash=action.args_hash,
            snapshot_hash=action.snapshot_hash,
            execution_owner=action.execution_owner,
            row_version=action.row_version,
            log_id=log_id,
        )


@dataclass(frozen=True, slots=True)
class LockedClaim:
    action: AiPreparedAction
    log: AiOperationLog
    claim: ClaimBinding
    execution_authorized: bool = False


@dataclass(frozen=True, slots=True)
class LockedCompletion:
    """Positive completion facts, with no execution credential or authority."""

    action: AiPreparedAction
    log: AiOperationLog


def _check_principal(action: AiPreparedAction, tenant: TenantContext) -> None:
    if action.tenant_id != tenant.tenant_id or action.user_id != tenant.actor_user_id:
        raise _invalid("操作与当前租户主体不一致")


def _check_action(
    action: AiPreparedAction,
    claim: ClaimBinding,
    tenant: TenantContext,
    *,
    require_running: bool,
) -> None:
    _check_principal(action, tenant)
    if (
        claim.tenant_id != tenant.tenant_id
        or claim.actor_user_id != tenant.actor_user_id
    ):
        raise _invalid("执行凭证与当前租户主体不一致")
    for field in (
        "tenant_id",
        "action_id",
        "confirmation_id",
        "execute_tool_call_id",
        "execute_tool_name",
        "conversation_id",
        "source_user_message_id",
        "trace_id",
        "agent_code",
        "args_hash",
        "snapshot_hash",
    ):
        if getattr(action, field) != getattr(claim, field):
            raise _invalid("操作与执行凭证绑定不一致")
    if (
        not is_protocol_owner(claim.execution_owner, claim.execute_tool_name)
        or not isinstance(action.frozen_args, dict)
        or not isinstance(action.snapshot, dict)
        or compute_args_hash(action.frozen_args) != claim.args_hash
        or compute_args_hash(action.snapshot) != claim.snapshot_hash
        or action.approved_by != claim.actor_user_id
        or action.approved_at is None
    ):
        raise _invalid("操作冻结内容或批准绑定无效")
    if action.status == "running":
        valid = (
            action.execution_owner == claim.execution_owner
            and action.row_version == claim.row_version
            and action.finished_at is None
        )
    elif not require_running and action.status in {"succeeded", "failed"}:
        valid = (
            action.row_version == claim.row_version + 1
            and action.finished_at is not None
            and action.execution_lease_expires_at is None
            and action.execution_owner
            == (None if action.status == "succeeded" else claim.execution_owner)
        )
    else:
        valid = False
    if action.status == "failed":
        valid = (
            valid
            and isinstance(action.error_code, str)
            and bool(_ERROR_RE.fullmatch(action.error_code))
        )
    elif action.status == "succeeded":
        valid = valid and action.error_code is None
    if not valid:
        raise _invalid("操作执行权或完成事实已变化")


def _check_log(
    action: AiPreparedAction, log: AiOperationLog, expected_status: str
) -> None:
    expected = {
        "tenant_id": action.tenant_id,
        "user_id": action.user_id,
        "conversation_id": action.conversation_id,
        "source_user_message_id": action.source_user_message_id,
        "trace_id": action.trace_id,
        "agent_code": action.agent_code,
        "tool_name": action.execute_tool_name,
        "tool_call_id": action.execute_tool_call_id,
        "confirmation_id": action.confirmation_id,
        "args_hash": action.args_hash,
        "status": expected_status,
        "readonly_snapshot": False,
        "execution_mode": "hitl",
        "approved_by": action.approved_by,
    }
    if any(getattr(log, field) != value for field, value in expected.items()):
        raise _invalid("必需操作日志与操作完整绑定不一致")
    if expected_status == "pending_confirmation":
        valid_time = log.started_at is None and log.finished_at is None
    elif expected_status == "running":
        valid_time = log.started_at is not None and log.finished_at is None
    else:
        valid_time = log.started_at is not None and log.finished_at is not None
    if not valid_time or log.error_code != action.error_code:
        raise _invalid("必需操作日志的完成事实不一致")


class AtomicCompletionService:
    """Lock and complete one exact claim inside the caller's transaction."""

    async def lock_terminal(
        self, db: AsyncSession, confirmation_id: str, *, tenant: TenantContext
    ) -> LockedCompletion:
        """Read a fully bound terminal after restart without inventing a claim."""
        await db.execute(select(func.set_config("lock_timeout", "2s", True)))
        reference = (
            await db.execute(
                select(
                    AiPreparedAction.action_id,
                    AiPreparedAction.conversation_id,
                    AiPreparedAction.source_user_message_id,
                ).where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.confirmation_id == confirmation_id,
                    AiPreparedAction.user_id == tenant.actor_user_id,
                )
            )
        ).one_or_none()
        if reference is None:
            raise _invalid("操作缺少可核对的完成身份")
        conversation = await db.scalar(
            select(AiConversation)
            .where(
                AiConversation.tenant_id == tenant.tenant_id,
                AiConversation.conversation_id == reference.conversation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        source = await db.scalar(
            select(AiMessage)
            .where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.message_id == reference.source_user_message_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        action = await db.scalar(
            select(AiPreparedAction)
            .where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.action_id == reference.action_id,
                AiPreparedAction.confirmation_id == confirmation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            action is None
            or conversation is None
            or source is None
            or action.conversation_id != reference.conversation_id
            or action.source_user_message_id != reference.source_user_message_id
            or conversation.user_id != tenant.actor_user_id
            or source.conversation_id != action.conversation_id
            or source.trace_id != action.trace_id
            or source.role != "user"
        ):
            raise _invalid("完成事实的会话或源消息绑定无效")
        _check_principal(action, tenant)
        if (
            action.status not in {"succeeded", "failed"}
            or action.finished_at is None
            or action.execution_lease_expires_at is not None
            or action.approved_by != tenant.actor_user_id
            or action.approved_at is None
            or not action.execute_tool_call_id
            or not action.agent_code
            or not action.trace_id
            or action.row_version <= 0
            or not isinstance(action.frozen_args, dict)
            or not isinstance(action.snapshot, dict)
            or compute_args_hash(action.frozen_args) != action.args_hash
            or compute_args_hash(action.snapshot) != action.snapshot_hash
        ):
            raise _invalid("操作完成事实不完整")
        if action.status == "succeeded":
            if action.execution_owner is not None or action.error_code is not None:
                raise _invalid("成功事实仍带有冲突的执行身份或错误")
            log_status = "success"
        else:
            if (
                not is_protocol_owner(action.execution_owner, action.execute_tool_name)
                or not isinstance(action.error_code, str)
                or not _ERROR_RE.fullmatch(action.error_code)
            ):
                raise _invalid("失败事实缺少可信来源或错误码")
            log_status = "failed"
        log = await self.lock_action_log(
            db, action, tenant=tenant, expected_status=log_status
        )
        return LockedCompletion(action=action, log=log)

    async def lock_action_log(
        self,
        db: AsyncSession,
        action: AiPreparedAction,
        *,
        tenant: TenantContext,
        expected_status: str,
    ) -> AiOperationLog:
        """Lock required log after the caller has locked context and action."""
        _check_principal(action, tenant)
        log = await db.scalar(
            select(AiOperationLog)
            .where(
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.tool_call_id == action.execute_tool_call_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if log is None:
            raise _invalid("操作缺少必需日志")
        _check_log(action, log, expected_status)
        return log

    async def lock_claim(
        self,
        db: AsyncSession,
        claim: ClaimBinding,
        *,
        tenant: TenantContext,
        require_active: bool = True,
        require_running: bool = True,
    ) -> LockedClaim:
        """Acquire fresh context -> source -> action -> log, then validate entry."""
        if (
            claim.tenant_id != tenant.tenant_id
            or claim.actor_user_id != tenant.actor_user_id
        ):
            raise _invalid("执行凭证与当前租户主体不一致")
        await db.execute(select(func.set_config("lock_timeout", "2s", True)))
        conversation = await db.scalar(
            select(AiConversation)
            .where(
                AiConversation.tenant_id == tenant.tenant_id,
                AiConversation.conversation_id == claim.conversation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        source = await db.scalar(
            select(AiMessage)
            .where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.message_id == claim.source_user_message_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        action = await db.scalar(
            select(AiPreparedAction)
            .where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.action_id == claim.action_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            conversation is None
            or source is None
            or action is None
            or conversation.user_id != claim.actor_user_id
            or source.conversation_id != claim.conversation_id
            or source.role != "user"
            or source.trace_id != claim.trace_id
            or (
                require_active
                and (conversation.deleted_at is not None or not source.is_active)
            )
        ):
            raise _invalid("操作的会话或源消息绑定无效")
        _check_action(action, claim, tenant, require_running=require_running)
        log_status = "success" if action.status == "succeeded" else action.status
        log = await self.lock_action_log(
            db, action, tenant=tenant, expected_status=log_status
        )
        if log.log_id != claim.log_id:
            raise _invalid("必需日志身份已变化")
        if require_running:
            now = await db.scalar(select(func.clock_timestamp()))
            lease = action.execution_lease_expires_at
            if lease is None or lease <= now:
                raise _invalid("执行认领租约已失效")
        return LockedClaim(
            action=action,
            log=log,
            claim=claim,
            execution_authorized=require_running and require_active,
        )

    @staticmethod
    def _check_completion(locked: LockedClaim, tenant: TenantContext) -> None:
        _check_action(locked.action, locked.claim, tenant, require_running=True)
        _check_log(locked.action, locked.log, "running")
        if locked.log.log_id != locked.claim.log_id:
            raise _invalid("必需日志身份已变化")

    async def _finish_log(
        self,
        db: AsyncSession,
        locked: LockedClaim,
        *,
        status: str,
        error_code: str | None,
        duration_ms: int,
        target_summary: str | None = None,
    ) -> None:
        claim = locked.claim
        values: dict[str, Any] = {
            "status": status,
            "error_code": error_code,
            "duration_ms": duration_ms,
            "finished_at": max(
                datetime.now(UTC).replace(tzinfo=None),
                locked.log.queued_at,
                locked.log.started_at,
            ),
            "result_summary": "prepared action succeeded"
            if status == "success"
            else "prepared action rolled back",
        }
        if target_summary is not None:
            values["target_summary"] = target_summary
        changed = await db.scalar(
            update(AiOperationLog)
            .where(
                AiOperationLog.log_id == claim.log_id,
                AiOperationLog.tenant_id == claim.tenant_id,
                AiOperationLog.user_id == claim.actor_user_id,
                AiOperationLog.conversation_id == claim.conversation_id,
                AiOperationLog.source_user_message_id == claim.source_user_message_id,
                AiOperationLog.trace_id == claim.trace_id,
                AiOperationLog.agent_code == claim.agent_code,
                AiOperationLog.tool_call_id == claim.execute_tool_call_id,
                AiOperationLog.tool_name == claim.execute_tool_name,
                AiOperationLog.args_hash == claim.args_hash,
                AiOperationLog.confirmation_id == claim.confirmation_id,
                AiOperationLog.approved_by == claim.actor_user_id,
                AiOperationLog.readonly_snapshot.is_(False),
                AiOperationLog.execution_mode == "hitl",
                AiOperationLog.status == "running",
                AiOperationLog.started_at.is_not(None),
                AiOperationLog.finished_at.is_(None),
            )
            .values(**values)
            .returning(AiOperationLog)
            .execution_options(populate_existing=True)
        )
        if changed is None:
            raise _invalid("必需日志完成 CAS 未命中")

    async def complete_success(
        self,
        db: AsyncSession,
        locked: LockedClaim,
        *,
        result: ToolResult,
        deps: ChatDeps,
        duration_ms: int,
        tenant: TenantContext,
    ) -> AiPreparedAction:
        """Persist already-sanitized result, lineage, PA and required success log."""
        self._check_completion(locked, tenant)
        if (
            not locked.execution_authorized
            or not result.ok
            or result.prepared_action is not None
            or deps.tenant != tenant
            or deps.user.user_id != tenant.actor_user_id
            or deps.user.tenant_id != tenant.tenant_id
            or deps.agent is None
            or deps.agent.code != locked.claim.agent_code
        ):
            raise _invalid("原子成功缺少一致的可信执行结果")
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
        ):
            raise _invalid("执行耗时无效")
        result_ui = _ui_to_dict(result.ui)
        json.dumps({"data": result.data, "ui": result_ui}, allow_nan=False)
        lineage = None
        target_summary = None
        if result.projection is not None:
            scope_hash = None
            if result.projection.scope_bound:
                current_user = await load_live_user_authority(db, tenant=tenant)
                scope_hash = await result_projection_service.compute_data_scope_hash(
                    db, current_user
                )
            lineage = result_projection_service.freeze_lineage(
                tenant=tenant,
                agent_code=locked.claim.agent_code,
                tool_codes=locked.action.tool_codes or [locked.claim.execute_tool_name],
                subject_refs=result.projection.subject_refs,
                data_scope_hash=scope_hash,
                projection_dependency_message_ids=(
                    locked.action.projection_dependency_message_ids or ()
                ),
            )
            target_summary = build_target_summary(lineage.subject_refs)
            if lineage.subject_refs and target_summary is None:
                raise _invalid("操作结果缺少可核对的安全对象引用")
        terminal = await prepared_action_service.transition_status(
            db,
            action_id=locked.claim.action_id,
            expected_status=PreparedActionStatus.RUNNING,
            expected_version=locked.claim.row_version,
            expected_execution_owner=locked.claim.execution_owner,
            target_status=PreparedActionStatus.SUCCEEDED,
            result_data=result.data,
            result_ui=result_ui,
            duration_ms=duration_ms,
            result_lineage=lineage,
            replace_result_lineage=True,
            tenant=tenant,
        )
        if terminal is None:
            raise _invalid("操作完成 CAS 未命中")
        await self._finish_log(
            db,
            locked,
            status="success",
            error_code=None,
            duration_ms=duration_ms,
            target_summary=target_summary,
        )
        await db.flush()
        return terminal

    async def complete_failed(
        self,
        db: AsyncSession,
        locked: LockedClaim,
        *,
        error_code: str,
        duration_ms: int,
        tenant: TenantContext,
    ) -> AiPreparedAction:
        """Finish only a DB claim after the Gateway has confirmed full rollback."""
        self._check_completion(locked, tenant)
        if locked.claim.execute_tool_name not in DB_TOOLS:
            raise _invalid("文件操作不能凭数据库回滚认定失败")
        if (
            not isinstance(error_code, str)
            or not _ERROR_RE.fullmatch(error_code)
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
        ):
            raise _invalid("失败完成事实无效")
        terminal = await prepared_action_service.transition_status(
            db,
            action_id=locked.claim.action_id,
            expected_status=PreparedActionStatus.RUNNING,
            expected_version=locked.claim.row_version,
            expected_execution_owner=locked.claim.execution_owner,
            retain_failed_owner=True,
            target_status=PreparedActionStatus.FAILED,
            error_code=error_code,
            duration_ms=duration_ms,
            tenant=tenant,
        )
        if terminal is None:
            raise _invalid("操作失败 CAS 未命中")
        await self._finish_log(
            db,
            locked,
            status="failed",
            error_code=error_code,
            duration_ms=duration_ms,
        )
        await db.flush()
        return terminal


atomic_completion_service = AtomicCompletionService()
