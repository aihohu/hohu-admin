"""Prepared-action freezing and confirmation-time verification."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException, BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.hitl.constants import (
    AiOperationStatus,
    PreparedActionStatus,
)
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmationPresentation
from app.modules.ai.schemas.conversation import (
    PendingActionOut,
    PendingActionStatusOut,
)
from app.modules.ai.service.result_projection_service import (
    ProjectionLineage,
    result_projection_service,
)


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


def _stable_positive_id(value: Any, *, field: str) -> str:
    if isinstance(value, bool):
        raise _binding_invalid(f"{field} contains an invalid identifier")
    if isinstance(value, int) and value > 0:
        return str(value)
    if (
        isinstance(value, str)
        and value.isdigit()
        and int(value) > 0
        and str(int(value)) == value
    ):
        return value
    raise _binding_invalid(f"{field} contains an invalid identifier")


def _stable_positive_id_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise _binding_invalid(f"{field} must be a complete identifier list")
    normalized = [_stable_positive_id(item, field=field) for item in value]
    if len(set(normalized)) != len(normalized):
        raise _binding_invalid(f"{field} contains duplicate identifiers")
    return normalized


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
        prepare_tool_name: str | None,
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
        tenant: TenantContext,
        conversation_id: int,
        source_user_message_id: int,
        trace_id: str,
        agent_code: str,
        expires_at: datetime,
        resolved_model_id: int | None,
        resolved_provider_id: int | None,
        data_scope_hash: str | None = None,
        projection_kind: str | None = None,
        guard_owner_token: str | None = None,
        command_action: str = "send",
        risk_level: str = "high",
        chip_target: str | None = None,
        projection_dependency_message_ids: list[int] | tuple[int, ...] = (),
        require_live_source: bool = False,
    ) -> AiPreparedAction:
        """Persist one immutable authorization proposal without committing."""
        if conversation_id <= 0 or source_user_message_id <= 0 or not trace_id:
            raise _binding_invalid("prepared action 缺少可信会话或 source message 绑定")
        if resolved_model_id is None or resolved_provider_id is None:
            raise _binding_invalid("prepared action 缺少冻结的 model/provider 绑定")
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
        if require_live_source and not await self.lock_source_binding(
            db,
            conversation_id=conversation_id,
            source_user_message_id=source_user_message_id,
            user_id=user_id,
            tenant=tenant,
        ):
            raise _binding_invalid("prepared action 的会话或源消息已失效")
        normalized_presentation = self.validate_presentation(presentation)

        business_snapshot_hash = canonical_payload_hash(snapshot)
        if snapshot_hash and snapshot_hash != business_snapshot_hash:
            raise _binding_invalid("业务方提供的 snapshot hash 与快照不一致")
        if _as_utc(expires_at) <= datetime.now(UTC):
            raise _snapshot_stale("prepared action 在创建确认前已过期")

        tool_codes = [execute_tool_name]
        if prepare_tool_call_id is not None:
            if not prepare_tool_name:
                raise _binding_invalid("prepared action 缺少稳定的 preview tool 绑定")
            tool_codes.append(prepare_tool_name)
        elif prepare_tool_name is not None:
            raise _binding_invalid("direct action 不得携带 preview tool 绑定")
        subject_refs = self._build_subject_refs(
            execute_tool_name=execute_tool_name,
            frozen_args=frozen_args,
            snapshot=snapshot,
            subject_ref=subject_ref,
            projection_kind=projection_kind,
        )
        if projection_kind == "scope_bound" and data_scope_hash is None:
            raise _binding_invalid("aggregate prepared action 缺少 data scope hash")
        lineage = result_projection_service.freeze_lineage(
            tenant=tenant,
            agent_code=agent_code,
            tool_codes=tool_codes,
            subject_refs=subject_refs,
            data_scope_hash=data_scope_hash,
            projection_dependency_message_ids=projection_dependency_message_ids,
        )
        canonical_snapshot = {
            **dict(snapshot),
            "_authorization": {
                "resolvedModelId": str(resolved_model_id),
                "resolvedProviderId": str(resolved_provider_id),
                "toolCodes": list(lineage.tool_codes),
                "subjectRefsHash": lineage.subject_refs_hash,
                "dataScopeHash": lineage.data_scope_hash,
                "resolverVersion": lineage.resolver_version,
                "projectionDependencyMessageIds": [
                    str(value) for value in lineage.projection_dependency_message_ids
                ],
            },
        }
        computed_snapshot_hash = canonical_payload_hash(canonical_snapshot)

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
            snapshot=canonical_snapshot,
            snapshot_hash=computed_snapshot_hash,
            subject_ref=dict(subject_ref) if subject_ref is not None else None,
            tool_codes=list(lineage.tool_codes),
            subject_refs=list(lineage.subject_refs),
            subject_refs_hash=lineage.subject_refs_hash,
            data_scope_hash=lineage.data_scope_hash,
            resolver_version=lineage.resolver_version,
            projection_dependency_message_ids=[
                str(value) for value in lineage.projection_dependency_message_ids
            ],
            presentation=normalized_presentation,
            user_id=user_id,
            tenant_id=tenant.tenant_id,
            conversation_id=conversation_id,
            source_user_message_id=source_user_message_id,
            trace_id=trace_id,
            agent_code=agent_code,
            resolved_model_id=resolved_model_id,
            resolved_provider_id=resolved_provider_id,
            guard_owner_token=guard_owner_token,
            command_action=command_action,
            risk_level=risk_level,
            chip_target=chip_target,
            expires_at=_as_utc(expires_at),
        )
        db.add(action)
        await db.flush()
        return action

    @staticmethod
    def _build_subject_refs(
        *,
        execute_tool_name: str,
        frozen_args: dict[str, Any],
        snapshot: dict[str, Any],
        subject_ref: dict[str, Any] | None,
        projection_kind: str | None,
    ) -> list[dict[str, Any]]:
        """Build complete stable targets from trusted frozen execution inputs."""
        if subject_ref is not None:
            return [dict(subject_ref)]
        if projection_kind in {"none", "scope_bound"}:
            return []
        if execute_tool_name in {"dept.create", "dept.update", "dept.move"}:
            business = snapshot.get("business")
            facts = business.get("facts") if isinstance(business, dict) else None
            if (
                not isinstance(business, dict)
                or business.get("version") != "phase3-dept-write/v1"
                or not isinstance(facts, dict)
            ):
                raise _binding_invalid(
                    "prepared department action lacks its Phase 3 authorization snapshot"
                )
            dept_ids = set(
                _stable_positive_id_list(
                    facts.get("deptIds"), field="department snapshot deptIds"
                )
            )
            direct_dept_fields = {
                "dept.create": ("parent_id",),
                "dept.update": ("dept_id",),
                "dept.move": ("dept_id", "new_parent_id"),
            }[execute_tool_name]
            for field_name in direct_dept_fields:
                value = frozen_args.get(field_name)
                if value is not None:
                    identifier = _stable_positive_id(value, field=field_name)
                    if identifier not in dept_ids:
                        raise _binding_invalid(
                            "prepared department action snapshot omits a direct target"
                        )

            impact = facts.get("impact")
            affected_roles = facts.get("affectedRoles")
            leader = facts.get("leader")
            if not isinstance(impact, dict) or not isinstance(affected_roles, list):
                raise _binding_invalid(
                    "prepared department action snapshot omits indirect impacts"
                )
            user_ids = {
                _stable_positive_id(value, field="department impact user")
                for value in impact
            }
            if leader is not None:
                if not isinstance(leader, dict) or "userId" not in leader:
                    raise _binding_invalid(
                        "prepared department action contains an invalid leader binding"
                    )
                user_ids.add(
                    _stable_positive_id(
                        leader["userId"], field="department leader user"
                    )
                )
            role_ids: set[str] = set()
            for item in affected_roles:
                if not isinstance(item, dict) or "roleId" not in item:
                    raise _binding_invalid(
                        "prepared department action contains an invalid role impact"
                    )
                role_ids.add(
                    _stable_positive_id(item["roleId"], field="department impact role")
                )
            return [
                *({"type": "dept", "id": value} for value in sorted(dept_ids, key=int)),
                *(
                    {"type": "managed_role", "id": value}
                    for value in sorted(role_ids, key=int)
                ),
                *({"type": "user", "id": value} for value in sorted(user_ids, key=int)),
            ]
        if execute_tool_name == "role.create":
            business = snapshot.get("business")
            if (
                not isinstance(business, dict)
                or business.get("version") != "phase3-role-write/v1"
            ):
                raise _binding_invalid(
                    "prepared role action lacks its Phase 3 authorization snapshot"
                )
            raw_dept_ids = frozen_args.get("dept_ids")
            dept_ids = (
                []
                if raw_dept_ids is None
                else _stable_positive_id_list(
                    raw_dept_ids, field="role create dept_ids"
                )
            )
            return [
                {"type": "dept", "id": value} for value in sorted(dept_ids, key=int)
            ]
        if execute_tool_name in {
            "role.update",
            "role.update_menus",
            "role.update_agents",
        }:
            business = snapshot.get("business")
            role_id = _stable_positive_id(frozen_args.get("role_id"), field="role_id")
            valid_snapshot = (
                isinstance(business, dict)
                and business.get("version") == "phase3-role-write/v1"
            )
            if execute_tool_name == "role.update_agents":
                target_role = (
                    business.get("targetRole") if isinstance(business, dict) else None
                )
                valid_snapshot = (
                    isinstance(target_role, dict)
                    and _stable_positive_id(
                        target_role.get("roleId"), field="targetRole.roleId"
                    )
                    == role_id
                    and isinstance(business.get("members"), list)
                )
            if not valid_snapshot:
                raise _binding_invalid(
                    "prepared role action lacks its Phase 3 authorization snapshot"
                )
            return [{"type": "managed_role", "id": role_id}]
        if execute_tool_name == "user.create":
            primary_dept_id = frozen_args.get("primary_dept_id")
            return (
                [{"type": "dept", "id": str(primary_dept_id)}]
                if primary_dept_id is not None
                else []
            )
        if execute_tool_name == "user.update_dept":
            user_id = frozen_args.get("user_id")
            assignments = frozen_args.get("dept_assignments")
            business = snapshot.get("business")
            old_assignments = (
                business.get("oldAssignments") if isinstance(business, dict) else None
            )
            if (
                not isinstance(user_id, int)
                or isinstance(user_id, bool)
                or not isinstance(assignments, list)
                or not isinstance(old_assignments, list)
            ):
                raise _binding_invalid(
                    "prepared department action lacks complete target bindings"
                )
            new_dept_ids: list[str] = []
            for item in assignments:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"dept_id", "is_primary"}
                    or not isinstance(item["dept_id"], int)
                    or isinstance(item["dept_id"], bool)
                    or not isinstance(item["is_primary"], bool)
                ):
                    raise _binding_invalid(
                        "prepared department action contains invalid new bindings"
                    )
                new_dept_ids.append(str(item["dept_id"]))
            old_dept_ids: list[str] = []
            for item in old_assignments:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"deptId", "isPrimary"}
                    or not isinstance(item["deptId"], str)
                    or not item["deptId"].isdigit()
                    or not isinstance(item["isPrimary"], bool)
                ):
                    raise _binding_invalid(
                        "prepared department action contains invalid old bindings"
                    )
                old_dept_ids.append(item["deptId"])
            dept_ids = set(new_dept_ids) | set(old_dept_ids)
            return [
                {"type": "user", "id": str(user_id)},
                *(
                    {"type": "dept", "id": dept_id}
                    for dept_id in sorted(dept_ids, key=int)
                ),
            ]
        if execute_tool_name == "user.update_roles":
            user_id = frozen_args.get("user_id")
            new_role_ids = frozen_args.get("role_ids")
            business = snapshot.get("business")
            old_role_ids = (
                business.get("oldRoleIds") if isinstance(business, dict) else None
            )
            if (
                not isinstance(user_id, int)
                or isinstance(user_id, bool)
                or user_id <= 0
                or not isinstance(new_role_ids, list)
                or not new_role_ids
                or any(
                    not isinstance(role_id, int)
                    or isinstance(role_id, bool)
                    or role_id <= 0
                    for role_id in new_role_ids
                )
                or len(set(new_role_ids)) != len(new_role_ids)
                or not isinstance(old_role_ids, list)
                or not old_role_ids
                or any(
                    not isinstance(role_id, str)
                    or not role_id.isdigit()
                    or int(role_id) <= 0
                    or str(int(role_id)) != role_id
                    for role_id in old_role_ids
                )
                or len(set(old_role_ids)) != len(old_role_ids)
            ):
                raise _binding_invalid(
                    "prepared role action lacks complete target bindings"
                )
            role_ids = {str(role_id) for role_id in new_role_ids} | set(old_role_ids)
            return [
                {"type": "user", "id": str(user_id)},
                {
                    "type": "complete_user_role_assignment",
                    "id": str(user_id),
                },
                *(
                    {"type": "delegable_role", "id": role_id}
                    for role_id in sorted(role_ids, key=int)
                ),
            ]
        scalar_fields = {
            "user.reset_password": ("user_id", "user"),
            "user.update": ("user_id", "user"),
        }
        scalar = scalar_fields.get(execute_tool_name)
        if scalar is not None:
            field_name, subject_type = scalar
            value = frozen_args.get(field_name)
            if value is None:
                raise _binding_invalid("prepared action 缺少稳定目标 ID")
            return [{"type": subject_type, "id": str(value)}]
        if execute_tool_name == "user.batch_delete":
            values = frozen_args.get("user_ids")
            if not isinstance(values, list) or not values:
                raise _binding_invalid("prepared action 缺少完整用户目标")
            return [{"type": "user", "id": str(value)} for value in values]
        raise _binding_invalid("prepared action tool 未声明授权目标契约")

    async def lock_source_binding(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
        source_user_message_id: int,
        user_id: int,
        tenant: TenantContext,
    ) -> bool:
        """Serialize action creation with conversation deletion."""
        conversation = (
            await db.execute(
                select(AiConversation)
                .where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.conversation_id == conversation_id,
                    AiConversation.user_id == user_id,
                    AiConversation.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conversation is None:
            return False
        source = (
            await db.execute(
                select(AiMessage)
                .where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.message_id == source_user_message_id,
                    AiMessage.conversation_id == conversation_id,
                    AiMessage.role == "user",
                    AiMessage.is_active.is_(True),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return source is not None

    async def get_by_confirmation_id(
        self,
        db: AsyncSession,
        confirmation_id: str,
        *,
        tenant: TenantContext,
    ) -> AiPreparedAction | None:
        return (
            await db.execute(
                select(AiPreparedAction).where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.confirmation_id == confirmation_id,
                )
            )
        ).scalar_one_or_none()

    async def get_by_execute_tool_call_id(
        self,
        db: AsyncSession,
        tool_call_id: str,
        *,
        tenant: TenantContext,
    ) -> AiPreparedAction | None:
        return (
            await db.execute(
                select(AiPreparedAction).where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.execute_tool_call_id == tool_call_id,
                )
            )
        ).scalar_one_or_none()

    async def lock_confirmation_context(
        self,
        db: AsyncSession,
        *,
        confirmation_id: str,
        tenant: TenantContext,
    ) -> PreparedConfirmationContext | None:
        """Lock conversation -> source message -> action in the canonical order."""
        action_ref = await self.get_by_confirmation_id(
            db, confirmation_id, tenant=tenant
        )
        if action_ref is None:
            return None
        conversation = (
            await db.execute(
                select(AiConversation)
                .where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.conversation_id == action_ref.conversation_id,
                    AiConversation.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        source = (
            await db.execute(
                select(AiMessage)
                .where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.message_id == action_ref.source_user_message_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        action = (
            await db.execute(
                select(AiPreparedAction)
                .where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.confirmation_id == confirmation_id,
                )
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
        execution_owner: str | None = None,
        execution_lease_expires_at: datetime | None = None,
        execution_lease_not_after: datetime | None = None,
        result_lineage: ProjectionLineage | None = None,
        replace_result_lineage: bool = False,
        tenant: TenantContext,
    ) -> AiPreparedAction | None:
        """CAS one legal action transition; zero rows means another winner."""
        if target_status not in _ALLOWED_TRANSITIONS.get(expected_status, set()):
            raise BusinessRuleException(
                f"prepared action 非法状态迁移 {expected_status} -> {target_status}",
                error_code="AI_PREPARED_ACTION_STATE_INVALID",
            )
        if result_lineage is not None and result_lineage.tenant_id != tenant.tenant_id:
            raise _binding_invalid("prepared action 结果 lineage 租户不一致")

        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "status": target_status.value,
            "row_version": expected_version + 1,
        }
        if target_status == PreparedActionStatus.APPROVED:
            values.update(approved_by=approved_by, approved_at=now)
        elif target_status == PreparedActionStatus.REJECTED:
            values.update(approved_by=approved_by, approved_at=now)
        elif target_status == PreparedActionStatus.RUNNING:
            if not execution_owner or execution_lease_expires_at is None:
                raise BusinessRuleException(
                    "prepared action 执行认领缺少 owner lease",
                    error_code="AI_PREPARED_ACTION_STATE_INVALID",
                )
            values.update(
                execution_owner=execution_owner,
                execution_lease_expires_at=_as_utc(execution_lease_expires_at),
            )
        if target_status.is_terminal:
            values.update(
                finished_at=now,
                error_code=error_code,
                result_data=result_data,
                result_ui=result_ui,
                duration_ms=duration_ms,
                execution_owner=None,
                execution_lease_expires_at=None,
            )
            if replace_result_lineage:
                values.update(
                    tool_codes=(
                        list(result_lineage.tool_codes) if result_lineage else None
                    ),
                    subject_refs=(
                        list(result_lineage.subject_refs) if result_lineage else None
                    ),
                    subject_refs_hash=(
                        result_lineage.subject_refs_hash if result_lineage else None
                    ),
                    data_scope_hash=(
                        result_lineage.data_scope_hash if result_lineage else None
                    ),
                    resolver_version=(
                        result_lineage.resolver_version if result_lineage else None
                    ),
                )

        conditions = [
            AiPreparedAction.action_id == action_id,
            AiPreparedAction.tenant_id == tenant.tenant_id,
            AiPreparedAction.status == expected_status.value,
            AiPreparedAction.row_version == expected_version,
        ]
        if execution_lease_not_after is not None:
            cutoff = _as_utc(execution_lease_not_after)
            conditions.append(
                or_(
                    AiPreparedAction.execution_lease_expires_at.is_(None),
                    AiPreparedAction.execution_lease_expires_at <= cutoff,
                )
            )
        stmt = (
            update(AiPreparedAction)
            .where(*conditions)
            .values(**values)
            .returning(AiPreparedAction)
            .execution_options(populate_existing=True)
        )
        return (await db.execute(stmt)).scalars().one_or_none()

    async def renew_execution_lease(
        self,
        db: AsyncSession,
        *,
        action_id: int,
        execution_owner: str,
        lease_expires_at: datetime,
        tenant: TenantContext,
    ) -> bool:
        """Extend a RUNNING action lease only for its current executor."""
        stmt = (
            update(AiPreparedAction)
            .where(
                AiPreparedAction.action_id == action_id,
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.status == PreparedActionStatus.RUNNING.value,
                AiPreparedAction.execution_owner == execution_owner,
            )
            .values(execution_lease_expires_at=_as_utc(lease_expires_at))
            .returning(AiPreparedAction.action_id)
        )
        return (await db.execute(stmt)).scalar_one_or_none() is not None

    async def list_pending_for_conversation(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
        user_id: int,
        tenant: TenantContext,
    ) -> list[AiPreparedAction]:
        """Return only live actions whose conversation and source remain owned/active."""
        stmt = (
            select(AiPreparedAction)
            .join(
                AiConversation,
                (AiConversation.tenant_id == AiPreparedAction.tenant_id)
                & (AiConversation.conversation_id == AiPreparedAction.conversation_id),
            )
            .join(
                AiMessage,
                (AiMessage.tenant_id == AiPreparedAction.tenant_id)
                & (AiMessage.message_id == AiPreparedAction.source_user_message_id),
            )
            .where(
                AiPreparedAction.conversation_id == conversation_id,
                AiPreparedAction.user_id == user_id,
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.status
                == PreparedActionStatus.PENDING_CONFIRMATION.value,
                AiPreparedAction.expires_at > datetime.now(UTC),
                AiConversation.user_id == user_id,
                AiConversation.deleted_at.is_(None),
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
        tenant: TenantContext,
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
                AiPreparedAction.tenant_id == tenant.tenant_id,
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

    async def expire_for_conversation_delete(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
        user_id: int,
        tenant: TenantContext,
    ) -> list[AiPreparedAction]:
        """Lock and terminalize deletable actions after the conversation lock."""
        nonterminal = (
            PreparedActionStatus.PREPARED.value,
            PreparedActionStatus.PENDING_CONFIRMATION.value,
            PreparedActionStatus.APPROVED.value,
            PreparedActionStatus.RUNNING.value,
        )
        actions = list(
            (
                await db.execute(
                    select(AiPreparedAction)
                    .where(
                        AiPreparedAction.conversation_id == conversation_id,
                        AiPreparedAction.user_id == user_id,
                        AiPreparedAction.tenant_id == tenant.tenant_id,
                        AiPreparedAction.status.in_(nonterminal),
                    )
                    .order_by(AiPreparedAction.action_id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        live_running = [
            action
            for action in actions
            if action.status == PreparedActionStatus.RUNNING.value
            and action.execution_lease_expires_at is not None
            and _as_utc(action.execution_lease_expires_at) > now
        ]
        if live_running:
            error = BusinessRuleException(
                "会话仍有正在执行的操作，执行终态化后再删除",
                error_code="AI_ACTION_RUNNING",
            )
            error.code = 409
            raise error

        for action in actions:
            if action.status == PreparedActionStatus.RUNNING.value:
                action.status = PreparedActionStatus.FAILED.value
                action.error_code = "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
            else:
                action.status = PreparedActionStatus.EXPIRED.value
                action.error_code = "AI_CONVERSATION_DELETED"
            action.row_version += 1
            action.finished_at = now
            action.execution_owner = None
            action.execution_lease_expires_at = None
            action.guard_owner_token = None

        actions_by_tool_call = {
            action.execute_tool_call_id: action for action in actions
        }
        tool_call_ids = list(actions_by_tool_call)
        if tool_call_ids:
            operation_logs = list(
                (
                    await db.execute(
                        select(AiOperationLog)
                        .where(
                            AiOperationLog.tenant_id == tenant.tenant_id,
                            AiOperationLog.user_id == user_id,
                            AiOperationLog.conversation_id == conversation_id,
                            AiOperationLog.tool_call_id.in_(tool_call_ids),
                            AiOperationLog.status.in_(
                                (
                                    AiOperationStatus.PENDING_CONFIRMATION.value,
                                    AiOperationStatus.RUNNING.value,
                                )
                            ),
                        )
                        .order_by(AiOperationLog.log_id.asc())
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for operation in operation_logs:
                action = actions_by_tool_call[operation.tool_call_id]
                if action.status == PreparedActionStatus.FAILED.value:
                    operation.status = AiOperationStatus.FAILED.value
                    operation.error_code = "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
                else:
                    operation.status = AiOperationStatus.EXPIRED.value
                    operation.error_code = "AI_CONVERSATION_DELETED"
                operation.finished_at = now.replace(tzinfo=None)
        return actions

    async def pending_source_is_valid(
        self,
        db: AsyncSession,
        action: AiPreparedAction,
        *,
        tenant: TenantContext,
    ) -> bool:
        """Check that a recoverable action still has its owned active source."""
        value = await db.scalar(
            select(AiMessage.message_id)
            .join(
                AiConversation,
                (AiConversation.tenant_id == AiMessage.tenant_id)
                & (AiConversation.conversation_id == AiMessage.conversation_id),
            )
            .where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.tenant_id == action.tenant_id,
                AiMessage.message_id == action.source_user_message_id,
                AiMessage.conversation_id == action.conversation_id,
                AiMessage.role == "user",
                AiMessage.is_active.is_(True),
                AiConversation.user_id == action.user_id,
                AiConversation.deleted_at.is_(None),
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

    async def project_pending_out(
        self,
        db: AsyncSession,
        *,
        action: AiPreparedAction,
        current_user: Any,
    ) -> PendingActionOut | PendingActionStatusOut:
        """Return a presentation only when its immutable lineage is still valid."""
        allowed = await result_projection_service.authorize_result_projection(
            db,
            current_user,
            owner_user_id=action.user_id,
            lineage=result_projection_service.lineage_from_record(action),
        )
        if allowed:
            return self.to_pending_out(action)
        return PendingActionStatusOut(
            confirmationId=action.confirmation_id,
            status=action.status,
            finishedAt=action.finished_at,
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
        result = validated.model_dump(by_alias=True, exclude_none=True)
        if not validated.summary_params:
            result.pop("summaryParams", None)
        if not validated.warning_keys:
            result.pop("warningKeys", None)
        return result

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
        self,
        db: AsyncSession,
        action: AiPreparedAction,
        *,
        tenant: TenantContext,
    ) -> None:
        """Revalidate business state needed to authorize the frozen execution."""
        if action.tenant_id != tenant.tenant_id:
            raise _snapshot_stale("prepared action 租户上下文已变化")
        if action.snapshot is None or action.snapshot_hash != canonical_payload_hash(
            action.snapshot
        ):
            raise _snapshot_stale("prepared action 快照已损坏")
        if action.execute_tool_name == "user.update_dept":
            frozen_args = action.frozen_args
            user_id = frozen_args.get("user_id")
            assignments = frozen_args.get("dept_assignments")
            precise_binding = (
                isinstance(user_id, int)
                and not isinstance(user_id, bool)
                and isinstance(assignments, list)
                and all(
                    isinstance(item, dict)
                    and set(item) == {"dept_id", "is_primary"}
                    and isinstance(item["dept_id"], int)
                    and not isinstance(item["dept_id"], bool)
                    and item["dept_id"] > 0
                    and isinstance(item["is_primary"], bool)
                    for item in assignments
                )
                and action.snapshot.get("argsHash") == action.args_hash
            )
            business_snapshot = action.snapshot.get("business")
            if not precise_binding or not isinstance(business_snapshot, dict):
                raise _snapshot_stale("用户部门调整缺少完整审批快照，请重新发起操作")

            from app.modules.system.service.user_department_assignment_service import (  # noqa: PLC0415
                user_department_assignment_service,
            )

            try:
                live_preview = (
                    await user_department_assignment_service.preview_departments(
                        db,
                        actor_user_id=action.user_id,
                        target_user_id=user_id,
                        dept_assignments=[
                            (item["dept_id"], item["is_primary"])
                            for item in assignments
                        ],
                        tenant=tenant,
                    )
                )
            except BusinessException as exc:
                raise _snapshot_stale(
                    "用户部门调整授权或目标事实已变化，请重新确认"
                ) from exc
            if live_preview.snapshot != business_snapshot:
                raise _snapshot_stale("用户部门调整授权或目标事实已变化，请重新确认")
            return
        if action.execute_tool_name == "user.update_roles":
            frozen_args = action.frozen_args
            user_id = frozen_args.get("user_id")
            role_ids = frozen_args.get("role_ids")
            precise_binding = (
                isinstance(user_id, int)
                and not isinstance(user_id, bool)
                and user_id > 0
                and isinstance(role_ids, list)
                and bool(role_ids)
                and all(
                    isinstance(role_id, int)
                    and not isinstance(role_id, bool)
                    and role_id > 0
                    for role_id in role_ids
                )
                and len(set(role_ids)) == len(role_ids)
                and action.snapshot.get("argsHash") == action.args_hash
                and action.args_hash == canonical_payload_hash(frozen_args)
            )
            business_snapshot = action.snapshot.get("business")
            if not precise_binding or not isinstance(business_snapshot, dict):
                raise _snapshot_stale("用户角色调整缺少完整审批快照，请重新发起操作")

            from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
                user_role_assignment_service,
            )

            try:
                live_preview = await user_role_assignment_service.preview_roles(
                    db,
                    actor_user_id=action.user_id,
                    target_user_id=user_id,
                    role_ids=role_ids,
                    tenant=tenant,
                )
            except BusinessException as exc:
                raise _snapshot_stale(
                    "用户角色调整授权或目标事实已变化，请重新确认"
                ) from exc
            if live_preview.snapshot != business_snapshot:
                raise _snapshot_stale("用户角色调整授权或目标事实已变化，请重新确认")
            return
        if action.execute_tool_name == "user.batch_delete":
            frozen_args = action.frozen_args
            user_ids = frozen_args.get("user_ids")
            precise_binding = (
                isinstance(user_ids, list)
                and bool(user_ids)
                and all(
                    isinstance(user_id, int) and not isinstance(user_id, bool)
                    for user_id in user_ids
                )
                and frozen_args.get("user_names") is None
                and frozen_args.get("phones") is None
                and action.snapshot.get("argsHash") == action.args_hash
            )
            business_snapshot = action.snapshot.get("business")
            if not precise_binding or not isinstance(business_snapshot, dict):
                raise _snapshot_stale("用户批量删除缺少精确目标绑定，请重新发起操作")

            from app.modules.system.service.user_service import (  # noqa: PLC0415
                user_service,
            )

            current_snapshot = await user_service.get_batch_delete_identity_snapshot(
                db, user_ids, tenant=tenant
            )
            if current_snapshot != business_snapshot:
                raise _snapshot_stale("用户批量删除目标身份已变化，请重新确认")
            return

        if action.execute_tool_name != "user.import_execute":
            return

        from app.modules.system.constants import (  # noqa: PLC0415
            ImportBatchStatus,
        )
        from app.modules.system.models.user_transfer import (  # noqa: PLC0415
            UserImportBatch,
        )

        subject_ref = action.subject_ref or {}
        if subject_ref.get("type") != "user_import_batch" or not subject_ref.get("id"):
            raise _snapshot_stale("用户导入 action 缺少批次引用")

        batch = (
            await db.execute(
                select(UserImportBatch).where(
                    UserImportBatch.tenant_id == tenant.tenant_id,
                    UserImportBatch.batch_id == str(subject_ref["id"]),
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
            "_authorization": action.snapshot.get("_authorization"),
        }
        if canonical_payload_hash(current_snapshot) != action.snapshot_hash:
            raise _snapshot_stale("用户导入 preview 快照已变化，请重新 preview")

    @staticmethod
    def validate_data_scope_snapshot(
        action: AiPreparedAction,
        *,
        current_data_scope_hash: str | None,
    ) -> None:
        """Reject a scope-bound action when its resolved authorization set drifted."""
        frozen_hash = action.data_scope_hash
        if frozen_hash is None:
            return
        if current_data_scope_hash != frozen_hash:
            raise _snapshot_stale("数据权限范围已变化，请重新发起操作")


prepared_action_service = PreparedActionService()
