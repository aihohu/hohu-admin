"""Prepared-action freezing and confirmation-time verification."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.models.prepared_action import AiPreparedAction


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """Return the existing type-aware canonical Gateway hash."""
    return compute_args_hash(payload)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _binding_invalid(message: str) -> BusinessRuleException:
    return BusinessRuleException(
        message, error_code="AI_PREPARED_ACTION_BINDING_INVALID"
    )


def _snapshot_stale(message: str) -> BusinessRuleException:
    return BusinessRuleException(
        message, error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE"
    )


class PreparedActionService:
    async def create_pending(
        self,
        db: AsyncSession,
        *,
        confirmation_id: str,
        prepare_tool_call_id: str | None,
        execute_tool_call_id: str,
        execute_tool_name: str,
        frozen_args: dict[str, Any],
        snapshot: dict[str, Any],
        snapshot_hash: str,
        subject_ref: dict[str, Any] | None,
        presentation: dict[str, Any],
        user_id: int,
        tenant_id: int,
        conversation_id: int,
        source_user_message_id: int,
        trace_id: str,
        agent_code: str,
        expires_at: datetime,
    ) -> AiPreparedAction:
        """Persist one immutable authorization proposal without committing."""
        if conversation_id <= 0 or source_user_message_id <= 0 or not trace_id:
            raise _binding_invalid("prepared action 缺少可信会话或 source message 绑定")
        if not frozen_args or not snapshot or not presentation:
            raise _binding_invalid("prepared action 冻结参数、快照或展示摘要为空")

        computed_snapshot_hash = canonical_payload_hash(snapshot)
        if snapshot_hash and snapshot_hash != computed_snapshot_hash:
            raise _binding_invalid("业务方提供的 snapshot hash 与快照不一致")
        if _as_utc(expires_at) <= datetime.now(UTC):
            raise _snapshot_stale("prepared action 在创建确认前已过期")

        action = AiPreparedAction(
            confirmation_id=confirmation_id,
            status="pending_confirmation",
            row_version=1,
            interaction_flow="prepared",
            requested_outcome="execute_if_approved",
            approval_mode="hitl",
            dispatch_mode="inline",
            prepare_tool_call_id=prepare_tool_call_id,
            execute_tool_call_id=execute_tool_call_id,
            execute_tool_name=execute_tool_name,
            frozen_args=dict(frozen_args),
            args_hash=canonical_payload_hash(frozen_args),
            snapshot=dict(snapshot),
            snapshot_hash=computed_snapshot_hash,
            subject_ref=dict(subject_ref) if subject_ref is not None else None,
            presentation=dict(presentation),
            user_id=user_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            source_user_message_id=source_user_message_id,
            trace_id=trace_id,
            agent_code=agent_code,
            expires_at=_as_utc(expires_at),
        )
        db.add(action)
        await db.flush()
        return action

    async def get_by_confirmation_id(
        self, db: AsyncSession, confirmation_id: str
    ) -> AiPreparedAction | None:
        return (
            await db.execute(
                select(AiPreparedAction).where(
                    AiPreparedAction.confirmation_id == confirmation_id
                )
            )
        ).scalar_one_or_none()

    def validate_pending_binding(
        self, action: AiPreparedAction, pending: PendingPayload
    ) -> None:
        """Reject any Redis payload that differs from the PostgreSQL fact."""
        if action.status != "pending_confirmation":
            raise _binding_invalid("prepared action 当前状态不可确认")
        if _as_utc(action.expires_at) <= datetime.now(UTC):
            raise _snapshot_stale("prepared action 已过期，请重新 preview")
        try:
            pending_expires_at = _as_utc(
                datetime.fromisoformat(pending.expires_at.replace("Z", "+00:00"))
            )
        except ValueError as exc:
            raise _binding_invalid("pending expiry 格式无效") from exc
        if _as_utc(action.expires_at) > pending_expires_at:
            raise _binding_invalid("prepared action 与 pending expiry 不一致")
        if action.args_hash != canonical_payload_hash(action.frozen_args):
            raise _binding_invalid("prepared action 冻结参数 hash 校验失败")
        if action.snapshot is None or action.snapshot_hash != canonical_payload_hash(
            action.snapshot
        ):
            raise _binding_invalid("prepared action 快照 hash 校验失败")

        trusted_binding = (
            action.user_id == pending.user_id
            and action.tenant_id == pending.tenant_id
            and action.conversation_id == pending.conversation_id
            and action.source_user_message_id == pending.source_user_message_id
            and action.execute_tool_call_id == pending.tool_call_id
            and action.execute_tool_name == pending.tool_name
            and action.trace_id == pending.trace_id
            and action.agent_code == pending.agent_code
            and action.args_hash == canonical_payload_hash(pending.args)
        )
        if not trusted_binding:
            raise _binding_invalid("prepared action 与待确认执行上下文不一致")

    async def validate_snapshot(
        self, db: AsyncSession, action: AiPreparedAction
    ) -> None:
        """Revalidate business state needed to authorize the frozen execution."""
        if action.snapshot is None or action.snapshot_hash != canonical_payload_hash(
            action.snapshot
        ):
            raise _snapshot_stale("prepared action 快照已损坏")
        if action.execute_tool_name != "user.import_execute":
            return

        from app.modules.system.user.constants import (  # noqa: PLC0415
            ImportBatchStatus,
        )
        from app.modules.system.user.models import UserImportBatch  # noqa: PLC0415

        subject_ref = action.subject_ref or {}
        if subject_ref.get("type") != "user_import_batch" or not subject_ref.get("id"):
            raise _snapshot_stale("用户导入 action 缺少批次引用")

        batch = (
            await db.execute(
                select(UserImportBatch).where(
                    UserImportBatch.batch_id == str(subject_ref["id"])
                )
            )
        ).scalar_one_or_none()
        frozen_args = action.frozen_args
        if (
            batch is None
            or batch.status != ImportBatchStatus.PREVIEW_DONE
            or batch.operator_id != action.user_id
            or batch.preview_token != frozen_args.get("preview_token")
            or batch.reason != frozen_args.get("reason")
            or batch.on_conflict != frozen_args.get("on_conflict")
        ):
            raise _snapshot_stale("用户导入批次或冻结策略已变化，请重新 preview")

        current_snapshot = {
            "batch_id": str(batch.batch_id),
            "file_sha256": batch.file_sha256,
            "records_hash": batch.records_hash,
            "operator_id": batch.operator_id,
            "total": batch.total_rows,
            "summary": {
                "new": batch.summary_new,
                "exists": batch.summary_exists,
                "conflict": batch.summary_conflict,
                "outOfScope": batch.summary_out_of_scope,
            },
        }
        if canonical_payload_hash(current_snapshot) != action.snapshot_hash:
            raise _snapshot_stale("用户导入 preview 快照已变化，请重新 preview")


prepared_action_service = PreparedActionService()
