"""Fail-closed authorization for persisted AI business-result projections."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.auth import has_explicit_permission
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.rbac import is_super_admin
from app.core.tenant import resolve_tenant_id
from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.constants import AI_CHAT_USE_PERMISSION
from app.modules.ai.core.data_scope_loader import build_data_scope_context
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.system.service.dept_service import dept_service
from app.modules.system.service.file_service import file_service
from app.modules.system.service.role_service import role_service
from app.modules.system.service.user_service import user_service
from app.modules.system.user.export_service import get_export_task
from app.modules.system.user.import_service import get_batch_detail
from app.utils.data_scope import DATA_SCOPE_UNION_RESOLVER_VERSION

DATA_SCOPE_RESOLVER_VERSION = DATA_SCOPE_UNION_RESOLVER_VERSION
RESULT_DOWNLOAD_TOKEN_TYPE = "ai_result_download"
RESULT_DOWNLOAD_TOKEN_TTL_SECONDS = 300
_RESULT_DOWNLOAD_SIGNING_CONTEXT = b"hohu:ai-result-download:v1\0"


def _canonical_hash(payload: dict[str, Any]) -> str:
    """Return the existing type-aware canonical SHA-256 representation."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=lambda value: f"{type(value).__qualname__}:{value!r}",
    ).encode()
    return sha256(encoded).hexdigest()


def _download_signing_key() -> bytes:
    """Derive a signing domain that cannot validate as an API access token."""
    return sha256(
        _RESULT_DOWNLOAD_SIGNING_CONTEXT + settings.SECRET_KEY.encode("utf-8")
    ).digest()


@dataclass(frozen=True)
class ProjectionLineage:
    """Immutable authorization facts used by every persisted-result reader."""

    tenant_id: int
    agent_code: str
    tool_codes: tuple[str, ...]
    subject_refs: tuple[dict[str, str], ...]
    subject_refs_hash: str
    data_scope_hash: str | None
    resolver_version: str
    projection_dependency_message_ids: tuple[int, ...] = ()


class ResultProjectionService:
    """Freeze and re-authorize AI result lineage without reading result payloads."""

    @staticmethod
    def _normalize_tool_codes(
        tool_codes: list[str] | tuple[str, ...],
    ) -> tuple[str, ...]:
        values = {str(value).strip() for value in tool_codes if str(value).strip()}
        return tuple(sorted(values))

    @staticmethod
    def normalize_subject_refs(
        subject_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, str], ...]:
        normalized: dict[tuple[str, str], dict[str, str]] = {}
        for value in subject_refs:
            if not isinstance(value, dict) or set(value) != {"type", "id"}:
                raise ValueError(
                    "projection subject refs require exact type and id keys"
                )
            subject_type = str(value["type"]).strip()
            subject_id = str(value["id"]).strip()
            if not subject_type or not subject_id:
                raise ValueError("projection subject refs cannot be blank")
            normalized[(subject_type, subject_id)] = {
                "type": subject_type,
                "id": subject_id,
            }
        return tuple(normalized[key] for key in sorted(normalized))

    @staticmethod
    def normalize_projection_dependency_message_ids(
        values: list[int | str] | tuple[int | str, ...],
    ) -> tuple[int, ...]:
        normalized = {int(value) for value in values}
        if any(value <= 0 for value in normalized):
            raise ValueError("projection dependency message IDs must be positive")
        return tuple(sorted(normalized))

    @staticmethod
    def subject_refs_hash(subject_refs: tuple[dict[str, str], ...]) -> str:
        return _canonical_hash({"subjectRefs": list(subject_refs)})

    def freeze_lineage(
        self,
        *,
        tenant_id: int,
        agent_code: str,
        tool_codes: list[str] | tuple[str, ...],
        subject_refs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        data_scope_hash: str | None = None,
        resolver_version: str = DATA_SCOPE_RESOLVER_VERSION,
        projection_dependency_message_ids: list[int | str] | tuple[int | str, ...] = (),
    ) -> ProjectionLineage:
        normalized_refs = self.normalize_subject_refs(subject_refs)
        return ProjectionLineage(
            tenant_id=int(tenant_id),
            agent_code=str(agent_code),
            tool_codes=self._normalize_tool_codes(tool_codes),
            subject_refs=normalized_refs,
            subject_refs_hash=self.subject_refs_hash(normalized_refs),
            data_scope_hash=data_scope_hash,
            resolver_version=resolver_version,
            projection_dependency_message_ids=(
                self.normalize_projection_dependency_message_ids(
                    projection_dependency_message_ids
                )
            ),
        )

    def lineage_from_record(
        self,
        record: Any,
        *,
        include_projection_dependencies: bool = True,
    ) -> ProjectionLineage | None:
        values = (
            getattr(record, "tenant_id", None),
            getattr(record, "agent_code", None),
            getattr(record, "tool_codes", None),
            getattr(record, "subject_refs", None),
            getattr(record, "subject_refs_hash", None),
            getattr(record, "resolver_version", None),
        )
        if any(value is None for value in values):
            return None
        try:
            normalized_refs = self.normalize_subject_refs(values[3])
            raw_dependencies = getattr(record, "projection_dependency_message_ids", ())
            if include_projection_dependencies and raw_dependencies is None:
                return None
            dependencies = (
                self.normalize_projection_dependency_message_ids(raw_dependencies)
                if include_projection_dependencies
                else ()
            )
        except (TypeError, ValueError):
            return None
        return ProjectionLineage(
            tenant_id=int(values[0]),
            agent_code=str(values[1]),
            tool_codes=self._normalize_tool_codes(values[2]),
            subject_refs=normalized_refs,
            subject_refs_hash=str(values[4]),
            data_scope_hash=getattr(record, "data_scope_hash", None),
            resolver_version=str(values[5]),
            projection_dependency_message_ids=dependencies,
        )

    @staticmethod
    def projection_dependency_message_ids(record: Any) -> tuple[int, ...] | None:
        """Return normalized immutable message dependencies, or None for legacy rows."""
        raw = getattr(record, "projection_dependency_message_ids", None)
        if raw is None or not isinstance(raw, list):
            return None
        try:
            values = {int(value) for value in raw}
        except (TypeError, ValueError):
            return None
        if any(value <= 0 for value in values):
            return None
        return tuple(sorted(values))

    async def compute_data_scope_hash(
        self,
        db: AsyncSession,
        user: Any,
        *,
        data_scope: Any | None = None,
    ) -> str:
        scope = data_scope or await build_data_scope_context(db, user)
        dept_ids: str | list[str] = (
            "all"
            if scope.accessible_dept_ids is None
            else sorted(str(value) for value in scope.accessible_dept_ids)
        )
        payload = {
            "resolverVersion": DATA_SCOPE_RESOLVER_VERSION,
            "isSuperAdmin": is_super_admin(user),
            "enabledRoleScopes": [
                {
                    "roleId": str(role.role_id),
                    "dataScope": str(role.data_scope),
                }
                for role in sorted(
                    (role for role in user.roles if role.status == STATUS_ENABLED),
                    key=lambda item: item.role_id,
                )
            ],
            "ownerUserId": str(user.user_id),
            "accessibleDeptIds": dept_ids,
            "userScope": (
                "all" if scope.accessible_user_scope is None else "dept_or_self"
            ),
        }
        return _canonical_hash(payload)

    @staticmethod
    def projection_hash(lineage: ProjectionLineage) -> str:
        """Bind a token to the complete immutable projection lineage."""
        return _canonical_hash(
            {
                "tenantId": lineage.tenant_id,
                "agentCode": lineage.agent_code,
                "toolCodes": list(lineage.tool_codes),
                "subjectRefs": list(lineage.subject_refs),
                "subjectRefsHash": lineage.subject_refs_hash,
                "dataScopeHash": lineage.data_scope_hash,
                "resolverVersion": lineage.resolver_version,
                "projectionDependencyMessageIds": list(
                    lineage.projection_dependency_message_ids
                ),
            }
        )

    async def issue_download_token(
        self,
        db: AsyncSession,
        user: Any,
        *,
        resource_type: str,
        resource_id: str,
        lineage: ProjectionLineage,
    ) -> str | None:
        """Issue a short-lived owner-bound token only after live authorization."""
        allowed = await self.authorize_result_projection(
            db,
            user,
            owner_user_id=user.user_id,
            lineage=lineage,
        )
        if not allowed:
            return None
        now = datetime.now(UTC)
        payload = {
            "type": RESULT_DOWNLOAD_TOKEN_TYPE,
            "sub": str(user.user_id),
            "tenantId": lineage.tenant_id,
            "resourceType": resource_type,
            "resourceId": str(resource_id),
            "projection": {
                "agentCode": lineage.agent_code,
                "toolCodes": list(lineage.tool_codes),
                "subjectRefs": list(lineage.subject_refs),
                "subjectRefsHash": lineage.subject_refs_hash,
                "dataScopeHash": lineage.data_scope_hash,
                "resolverVersion": lineage.resolver_version,
                "projectionDependencyMessageIds": list(
                    lineage.projection_dependency_message_ids
                ),
            },
            "projectionHash": self.projection_hash(lineage),
            "iat": now,
            "exp": now + timedelta(seconds=RESULT_DOWNLOAD_TOKEN_TTL_SECONDS),
        }
        return jwt.encode(
            payload, _download_signing_key(), algorithm=settings.ALGORITHM
        )

    def read_download_token(
        self,
        token: str,
        user: Any,
        *,
        resource_type: str,
        resource_id: str,
    ) -> ProjectionLineage | None:
        """Validate token identity, resource binding, and projection integrity."""
        try:
            payload = jwt.decode(
                token,
                _download_signing_key(),
                algorithms=[settings.ALGORITHM],
            )
            if payload.get("type") != RESULT_DOWNLOAD_TOKEN_TYPE:
                return None
            if int(payload.get("sub")) != int(user.user_id):
                return None
            if int(payload.get("tenantId")) != resolve_tenant_id(user):
                return None
            if payload.get("resourceType") != resource_type:
                return None
            if str(payload.get("resourceId")) != str(resource_id):
                return None
            projection = payload["projection"]
            lineage = self.freeze_lineage(
                tenant_id=int(payload["tenantId"]),
                agent_code=projection["agentCode"],
                tool_codes=projection["toolCodes"],
                subject_refs=projection["subjectRefs"],
                data_scope_hash=projection.get("dataScopeHash"),
                resolver_version=projection["resolverVersion"],
                projection_dependency_message_ids=projection.get(
                    "projectionDependencyMessageIds", []
                ),
            )
            if lineage.subject_refs_hash != projection["subjectRefsHash"]:
                return None
            if self.projection_hash(lineage) != payload.get("projectionHash"):
                return None
            return lineage
        except (JWTError, KeyError, TypeError, ValueError):
            return None

    async def refresh_download_urls(
        self,
        db: AsyncSession,
        user: Any,
        *,
        lineage: ProjectionLineage,
        value: Any,
        resource_ids: list[str] | None = None,
    ) -> Any:
        """Replace persisted AI export URLs with newly authorized short tokens."""
        export_ids = {
            subject["id"]
            for subject in lineage.subject_refs
            if subject["type"] == "user_export_task"
        }
        export_ids.update(str(value) for value in (resource_ids or []) if value)
        if not export_ids:
            return value

        urls: dict[str, str] = {}
        for export_id in sorted(export_ids):
            token_lineage = self.freeze_lineage(
                tenant_id=lineage.tenant_id,
                agent_code=lineage.agent_code,
                tool_codes=lineage.tool_codes,
                subject_refs=[
                    *lineage.subject_refs,
                    {"type": "user_export_task", "id": export_id},
                ],
                data_scope_hash=lineage.data_scope_hash,
                resolver_version=lineage.resolver_version,
                projection_dependency_message_ids=(
                    lineage.projection_dependency_message_ids
                ),
            )
            token = await self.issue_download_token(
                db,
                user,
                resource_type="user_export",
                resource_id=export_id,
                lineage=token_lineage,
            )
            if token is not None:
                urls[export_id] = f"/ai/download/user-export/{export_id}?token={token}"

        def replace(current: Any) -> Any:
            if isinstance(current, list):
                return [replace(item) for item in current]
            if isinstance(current, dict):
                return {key: replace(item) for key, item in current.items()}
            if isinstance(current, str):
                for export_id, url in urls.items():
                    if f"/ai/download/user-export/{export_id}" in current:
                        return url
            return current

        return replace(value)

    async def authorize_result_projection(
        self,
        db: AsyncSession,
        user: Any,
        *,
        owner_user_id: int,
        lineage: ProjectionLineage | None,
    ) -> bool:
        if lineage is None:
            return False
        if int(owner_user_id) != int(user.user_id):
            return False
        if lineage.tenant_id != resolve_tenant_id(user):
            return False
        if not has_explicit_permission(user, AI_CHAT_USE_PERMISSION):
            return False
        if self.subject_refs_hash(lineage.subject_refs) != lineage.subject_refs_hash:
            return False
        if lineage.resolver_version != DATA_SCOPE_RESOLVER_VERSION:
            return False
        if not await self._authorize_agent_and_tools(db, user, lineage):
            return False
        if lineage.data_scope_hash is not None:
            current_hash = await self.compute_data_scope_hash(db, user)
            if current_hash != lineage.data_scope_hash:
                return False
        if not await self._authorize_subjects(db, user, lineage):
            return False
        return await self._authorize_projection_dependencies(
            db,
            user,
            owner_user_id=owner_user_id,
            dependency_message_ids=lineage.projection_dependency_message_ids,
        )

    async def authorize_message_projection(
        self,
        db: AsyncSession,
        user: Any,
        *,
        owner_user_id: int,
        message: AiMessage | Any,
    ) -> bool:
        """Authorize a message and every prior assistant projection used as context."""
        dependencies = self.projection_dependency_message_ids(message)
        if dependencies is None:
            return False
        if not await self.authorize_result_projection(
            db,
            user,
            owner_user_id=owner_user_id,
            lineage=self.lineage_from_record(
                message,
                include_projection_dependencies=False,
            ),
        ):
            return False

        root_id = int(message.message_id)
        conversation_id = int(message.conversation_id)
        if root_id in dependencies:
            return False

        visited: set[int] = set()
        pending = set(dependencies)
        while pending:
            batch = pending - visited
            if not batch:
                break
            rows = (
                (
                    await db.execute(
                        select(AiMessage).where(
                            AiMessage.conversation_id == conversation_id,
                            AiMessage.message_id.in_(batch),
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_id = {int(item.message_id): item for item in rows}
            if set(by_id) != batch:
                return False
            for dependency in rows:
                if dependency.role == "user":
                    return False
                nested = self.projection_dependency_message_ids(dependency)
                if nested is None or root_id in nested:
                    return False
                if not await self.authorize_result_projection(
                    db,
                    user,
                    owner_user_id=owner_user_id,
                    lineage=self.lineage_from_record(
                        dependency,
                        include_projection_dependencies=False,
                    ),
                ):
                    return False
                pending.update(nested)
            visited.update(batch)
        return True

    async def collect_message_projection_dependencies(
        self,
        db: AsyncSession,
        *,
        conversation_id: int,
    ) -> list[int]:
        """Freeze every prior active assistant projection as a transitive dependency."""
        messages = (
            (
                await db.execute(
                    select(AiMessage).where(
                        AiMessage.conversation_id == conversation_id,
                        AiMessage.role != "user",
                        AiMessage.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        dependencies: set[int] = set()
        for message in messages:
            dependencies.add(int(message.message_id))
            nested = self.projection_dependency_message_ids(message)
            if nested is not None:
                dependencies.update(nested)
        return sorted(dependencies)

    async def _authorize_projection_dependencies(
        self,
        db: AsyncSession,
        user: Any,
        *,
        owner_user_id: int,
        dependency_message_ids: tuple[int, ...],
    ) -> bool:
        visited: set[int] = set()
        pending = set(dependency_message_ids)
        while pending:
            batch = pending - visited
            if not batch:
                break
            rows = (
                (
                    await db.execute(
                        select(AiMessage)
                        .join(
                            AiConversation,
                            AiConversation.conversation_id == AiMessage.conversation_id,
                        )
                        .where(
                            AiMessage.message_id.in_(batch),
                            AiConversation.user_id == owner_user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_id = {int(item.message_id): item for item in rows}
            if set(by_id) != batch:
                return False
            for dependency in rows:
                if dependency.role == "user":
                    return False
                nested = self.projection_dependency_message_ids(dependency)
                if nested is None:
                    return False
                if not await self.authorize_result_projection(
                    db,
                    user,
                    owner_user_id=owner_user_id,
                    lineage=self.lineage_from_record(
                        dependency,
                        include_projection_dependencies=False,
                    ),
                ):
                    return False
                pending.update(nested)
            visited.update(batch)
        return True

    async def _authorize_agent_and_tools(
        self,
        db: AsyncSession,
        user: Any,
        lineage: ProjectionLineage,
    ) -> bool:
        try:
            await agent_authorization_service.authorize_agent_access(
                db, user, lineage.agent_code
            )
        except BusinessException:
            return False
        permissions = agent_authorization_service.tool_permissions(user)
        enabled_extra = set(
            await get_ai_config_str_list(db, "ai:enabled_tools", default=[])
        )
        registry = ToolRegistry.get()
        for tool_code in lineage.tool_codes:
            registered = registry.find(tool_code)
            if registered is None or registered.meta.agent != lineage.agent_code:
                return False
            meta = registered.meta
            if not set(meta.required_perms) <= permissions:
                return False
            if not meta.default_enabled and meta.name not in enabled_extra:
                return False
            if not meta.llm_visible:
                source = registry.prepared_source_for(meta.name)
                if source is None or not set(source.meta.required_perms) <= permissions:
                    return False
                if (
                    not source.meta.default_enabled
                    and source.meta.name not in enabled_extra
                ):
                    return False
        return True

    async def _authorize_subjects(
        self,
        db: AsyncSession,
        user: Any,
        lineage: ProjectionLineage,
    ) -> bool:
        if not lineage.subject_refs:
            return True
        delegable_role_ids: list[int] = []
        managed_role_ids: list[int] = []
        complete_role_assignment_user_ids: list[int] = []
        role_assignment_access_user_ids: list[int] = []
        try:
            for subject in lineage.subject_refs:
                subject_type = subject["type"]
                if subject_type == "delegable_role":
                    delegable_role_ids.append(int(subject["id"]))
                elif subject_type == "managed_role":
                    managed_role_ids.append(int(subject["id"]))
                elif subject_type == "complete_user_role_assignment":
                    complete_role_assignment_user_ids.append(int(subject["id"]))
                elif subject_type == "user_role_assignment_access":
                    role_assignment_access_user_ids.append(int(subject["id"]))

            if (
                delegable_role_ids
                or managed_role_ids
                or complete_role_assignment_user_ids
                or role_assignment_access_user_ids
            ):
                from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
                    user_role_assignment_service,
                )

            if managed_role_ids:
                from app.modules.system.service.role_management_service import (  # noqa: PLC0415
                    role_management_service,
                )

            if delegable_role_ids and not (
                await user_role_assignment_service.roles_are_assignable(
                    db,
                    actor_user_id=int(user.user_id),
                    role_ids=delegable_role_ids,
                )
            ):
                return False
            for role_id in managed_role_ids:
                await role_management_service.authorize_role_projection(
                    db,
                    actor_user_id=int(user.user_id),
                    role_id=role_id,
                )
            for target_user_id in complete_role_assignment_user_ids:
                (
                    _roles,
                    complete,
                ) = await user_role_assignment_service.get_complete_assignable_roles(
                    db,
                    actor_user_id=int(user.user_id),
                    target_user_id=target_user_id,
                )
                if not complete:
                    return False
            for target_user_id in role_assignment_access_user_ids:
                await user_role_assignment_service.ensure_role_assignment_access(
                    db,
                    actor_user_id=int(user.user_id),
                    target_user_id=target_user_id,
                )
        except (BusinessException, TypeError, ValueError):
            return False

        scope = await build_data_scope_context(db, user)
        for subject in lineage.subject_refs:
            if subject["type"] in {
                "delegable_role",
                "managed_role",
                "complete_user_role_assignment",
                "user_role_assignment_access",
            }:
                continue
            if not await self._authorize_subject(
                db,
                user,
                scope=scope,
                tenant_id=lineage.tenant_id,
                subject=subject,
            ):
                return False
        return True

    async def _authorize_subject(
        self,
        db: AsyncSession,
        user: Any,
        *,
        scope: Any,
        tenant_id: int,
        subject: dict[str, str],
    ) -> bool:
        subject_type = subject["type"]
        subject_id = subject["id"]
        try:
            if subject_type == "user":
                user_id = int(subject_id)
                if scope.accessible_user_scope is not None:
                    user_scope = scope.accessible_user_scope.subquery()
                    visible = await db.scalar(
                        select(user_scope.c.user_id)
                        .where(user_scope.c.user_id == user_id)
                        .limit(1)
                    )
                    return visible is not None
                return await user_service.user_exists(db, user_id)
            if subject_type == "dept":
                dept_id = int(subject_id)
                if scope.accessible_dept_ids is not None:
                    return dept_id in scope.accessible_dept_ids
                await dept_service.get_by_id(db, dept_id)
                return True
            if subject_type == "role":
                await role_service.get_role_detail(db, int(subject_id))
                return True
            if subject_type == "file":
                await file_service.get_by_id(
                    db,
                    int(subject_id),
                    tenant_id=tenant_id,
                    owner_user_id=user.user_id,
                )
                return True
            if subject_type == "user_import_batch":
                batch, _operator_name = await get_batch_detail(db, subject_id)
                return batch is not None and batch.operator_id == user.user_id
            if subject_type == "user_export_task":
                task = await get_export_task(
                    db,
                    subject_id,
                    operator_id=user.user_id,
                    allow_cross_owner=False,
                )
                return task is not None
        except (BusinessException, TypeError, ValueError):
            return False
        return False

    async def lineage_for_operation_log(
        self,
        db: AsyncSession,
        log: AiOperationLog,
    ) -> ProjectionLineage | None:
        action = (
            await db.execute(
                select(AiPreparedAction).where(
                    AiPreparedAction.execute_tool_call_id == log.tool_call_id
                )
            )
        ).scalar_one_or_none()
        if action is not None:
            return self.lineage_from_record(action)
        messages = (
            await db.execute(
                select(AiMessage).where(
                    AiMessage.conversation_id == log.conversation_id,
                    AiMessage.trace_id == log.trace_id,
                    AiMessage.role == "assistant",
                    AiMessage.is_active.is_(True),
                )
            )
        ).scalars()
        for message in messages:
            lineage = self.lineage_from_record(message)
            if lineage is not None and log.tool_name in lineage.tool_codes:
                return lineage
        return None


result_projection_service = ResultProjectionService()
