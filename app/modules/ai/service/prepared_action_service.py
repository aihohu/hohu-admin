"""Prepared-action freezing and confirmation-time verification."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.hitl.constants import PreparedActionStatus
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmationPresentation
from app.modules.ai.schemas.conversation import PendingActionOut


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


_ALLOWED_TRANSITIONS: dict[PreparedActionStatus, set[PreparedActionStatus]] = {
    PreparedActionStatus.PREPARED: {
        PreparedActionStatus.PENDING_CONFIRMATION,
        PreparedActionStatus.EXPIRED,
    },
    PreparedActionStatus.PENDING_CONFIRMATION: {
        PreparedActionStatus.APPROVED,
        PreparedActionStatus.REJECTED,
        PreparedActionStatus.EXPIRED,
    },
    PreparedActionStatus.APPROVED: {
        PreparedActionStatus.RUNNING,
        PreparedActionStatus.FAILED,
    },
    PreparedActionStatus.RUNNING: {
        PreparedActionStatus.SUCCEEDED,
        PreparedActionStatus.FAILED,
    },
}


@dataclass(frozen=True)
class PreparedConfirmationContext:
    action: AiPreparedAction
    conversation: AiConversation
    source_message: AiMessage


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
        interaction_flow: str = "prepared",
        requested_outcome: str = "execute_if_approved",
        user_id: int,
        tenant_id: int,
        conversation_id: int,
        source_user_message_id: int,
        trace_id: str,
        agent_code: str,
        expires_at: datetime,
        guard_owner_token: str | None = None,
        command_action: str = "send",
        risk_level: str = "high",
        chip_target: str | None = None,
    ) -> AiPreparedAction:
        """Persist one immutable authorization proposal without committing."""
        if conversation_id <= 0 or source_user_message_id <= 0 or not trace_id:
            raise _binding_invalid("prepared action 缺少可信会话或 source message 绑定")
        if not snapshot or not presentation:
            raise _binding_invalid("action 快照或展示摘要为空")
        if interaction_flow not in {"direct", "prepared"}:
            raise _binding_invalid("action interaction flow 无效")
        if (
            interaction_flow == "prepared"
            and requested_outcome != "execute_if_approved"
        ):
            raise _binding_invalid("prepared action outcome 无效")
        if interaction_flow == "direct" and requested_outcome != "direct":
            raise _binding_invalid("direct action outcome 无效")
        normalized_presentation = self.validate_presentation(presentation)

        computed_snapshot_hash = canonical_payload_hash(snapshot)
        if snapshot_hash and snapshot_hash != computed_snapshot_hash:
            raise _binding_invalid("业务方提供的 snapshot hash 与快照不一致")
        if _as_utc(expires_at) <= datetime.now(UTC):
            raise _snapshot_stale("prepared action 在创建确认前已过期")

        action = AiPreparedAction(
            confirmation_id=confirmation_id,
            status="pending_confirmation",
            row_version=1,
            interaction_flow=interaction_flow,
            requested_outcome=requested_outcome,
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
            presentation=normalized_presentation,
            user_id=user_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            source_user_message_id=source_user_message_id,
            trace_id=trace_id,
            agent_code=agent_code,
            guard_owner_token=guard_owner_token,
            command_action=command_action,
            risk_level=risk_level,
            chip_target=chip_target,
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

    async def get_by_execute_tool_call_id(
        self, db: AsyncSession, tool_call_id: str
    ) -> AiPreparedAction | None:
        return (
            await db.execute(
                select(AiPreparedAction).where(
                    AiPreparedAction.execute_tool_call_id == tool_call_id
                )
            )
        ).scalar_one_or_none()

    async def lock_confirmation_context(
        self,
        db: AsyncSession,
        *,
        confirmation_id: str,
    ) -> PreparedConfirmationContext | None:
        """Lock conversation -> source message -> action in the canonical order."""
        action_ref = await self.get_by_confirmation_id(db, confirmation_id)
        if action_ref is None:
            return None
        conversation = (
            await db.execute(
                select(AiConversation)
                .where(AiConversation.conversation_id == action_ref.conversation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        source = (
            await db.execute(
                select(AiMessage)
                .where(AiMessage.message_id == action_ref.source_user_message_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        action = (
            await db.execute(
                select(AiPreparedAction)
                .where(AiPreparedAction.confirmation_id == confirmation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if conversation is None or source is None or action is None:
            return None
        return PreparedConfirmationContext(action, conversation, source)

    async def transition_status(
        self,
        db: AsyncSession,
        *,
        action_id: int,
        expected_status: PreparedActionStatus,
        expected_version: int,
        target_status: PreparedActionStatus,
        approved_by: int | None = None,
        error_code: str | None = None,
        result_data: Any = None,
        result_ui: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> AiPreparedAction | None:
        """CAS one legal action transition; zero rows means another winner."""
        if target_status not in _ALLOWED_TRANSITIONS.get(expected_status, set()):
            raise BusinessRuleException(
                f"prepared action 非法状态迁移 {expected_status} -> {target_status}",
                error_code="AI_PREPARED_ACTION_STATE_INVALID",
            )

        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "status": target_status.value,
            "row_version": expected_version + 1,
        }
        if target_status == PreparedActionStatus.APPROVED:
            values.update(approved_by=approved_by, approved_at=now)
        elif target_status == PreparedActionStatus.REJECTED:
            values.update(approved_by=approved_by, approved_at=now)
        if target_status.is_terminal:
            values.update(
                finished_at=now,
                error_code=error_code,
                result_data=result_data,
                result_ui=result_ui,
                duration_ms=duration_ms,
            )

        stmt = (
            update(AiPreparedAction)
            .where(
                AiPreparedAction.action_id == action_id,
                AiPreparedAction.status == expected_status.value,
                AiPreparedAction.row_version == expected_version,
            )
            .values(**values)
            .returning(AiPreparedAction)
            .execution_options(populate_existing=True)
        )
        return (await db.execute(stmt)).scalars().one_or_none()

    async def list_pending_for_conversation(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
        user_id: int,
        tenant_id: int,
    ) -> list[AiPreparedAction]:
        """Return only live actions whose conversation and source remain owned/active."""
        stmt = (
            select(AiPreparedAction)
            .join(
                AiConversation,
                AiConversation.conversation_id == AiPreparedAction.conversation_id,
            )
            .join(
                AiMessage,
                AiMessage.message_id == AiPreparedAction.source_user_message_id,
            )
            .where(
                AiPreparedAction.conversation_id == conversation_id,
                AiPreparedAction.user_id == user_id,
                AiPreparedAction.tenant_id == tenant_id,
                AiPreparedAction.status
                == PreparedActionStatus.PENDING_CONFIRMATION.value,
                AiPreparedAction.expires_at > datetime.now(UTC),
                AiConversation.user_id == user_id,
                AiMessage.conversation_id == conversation_id,
                AiMessage.role == "user",
                AiMessage.is_active.is_(True),
            )
            .order_by(
                AiPreparedAction.created_at.asc(), AiPreparedAction.action_id.asc()
            )
        )
        return list((await db.execute(stmt)).scalars().all())

    async def has_in_progress_for_conversation(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
        user_id: int,
        tenant_id: int,
    ) -> bool:
        statuses = (
            PreparedActionStatus.PREPARED.value,
            PreparedActionStatus.PENDING_CONFIRMATION.value,
            PreparedActionStatus.APPROVED.value,
            PreparedActionStatus.RUNNING.value,
        )
        value = await db.scalar(
            select(AiPreparedAction.action_id)
            .where(
                AiPreparedAction.conversation_id == conversation_id,
                AiPreparedAction.user_id == user_id,
                AiPreparedAction.tenant_id == tenant_id,
                AiPreparedAction.status.in_(statuses),
                or_(
                    AiPreparedAction.status
                    != PreparedActionStatus.PENDING_CONFIRMATION.value,
                    AiPreparedAction.expires_at > datetime.now(UTC),
                ),
            )
            .limit(1)
        )
        return value is not None

    @staticmethod
    def to_pending_out(action: AiPreparedAction) -> PendingActionOut:
        return PendingActionOut(
            action_id=action.action_id,
            confirmation_id=action.confirmation_id,
            source_user_message_id=action.source_user_message_id,
            trace_id=action.trace_id,
            tool=action.execute_tool_name,
            tool_call_id=action.execute_tool_call_id,
            source_tool_call_id=action.prepare_tool_call_id,
            interaction_flow=action.interaction_flow,
            presentation=action.presentation,
            expires_at=action.expires_at,
        )

    @staticmethod
    def validate_presentation(presentation: dict[str, Any]) -> dict[str, Any]:
        """Return the canonical ordered DTO or fail closed."""
        try:
            validated = ConfirmationPresentation.model_validate(presentation)
        except ValidationError as exc:
            raise _binding_invalid(
                "confirmation presentation 无效或含敏感字段"
            ) from exc
        return validated.model_dump(exclude_none=True)

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

        if not batch.file_storage_key:
            raise _snapshot_stale("用户导入预检文件已丢失，请重新 preview")
        from app.core.file_storage import get_file_storage  # noqa: PLC0415

        try:
            file_bytes = await get_file_storage().read(batch.file_storage_key)
        except FileNotFoundError as exc:
            raise _snapshot_stale("用户导入预检文件已丢失，请重新 preview") from exc
        if sha256(file_bytes).hexdigest() != batch.file_sha256:
            raise _snapshot_stale("用户导入预检文件内容已变化，请重新 preview")

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
