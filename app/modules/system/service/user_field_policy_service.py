"""Tenant-scoped candidate persistence, deliberately absent from public routes.

The caller owns authentication, policy-management permission, current security
versions and the outer transaction. Nothing here makes a candidate effective.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import SessionTransaction

from app.core.exceptions import (
    BusinessException,
    InvalidParameterException,
    NotFoundException,
)
from app.core.tenant import TenantContext
from app.core.tenant_scope import require_tenant_context
from app.modules.system.field_policy.documents import (
    canonical_rules,
    catalog_digest,
    document_digest,
    read_rules,
    rules_digest,
)
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.user_fields import CATALOG_REVISION
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyAudit,
    UserFieldPolicyBinding,
    UserFieldPolicyRevision,
    UserFieldPolicyState,
)
from app.modules.system.service.field_policy_barrier_service import (
    field_policy_barrier_service,
    lock_order_error,
    unavailable,
)

_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_VERSION = 2**63 - 1
_MAX_BINDINGS = 1024
_WRITE_TARGET_KEY = "user_field_policy_write_target"


def _invalid() -> InvalidParameterException:
    return InvalidParameterException(
        "用户字段策略输入无效或超过限制", error_code="FIELD_POLICY_INVALID"
    )


def _conflict() -> BusinessException:
    return BusinessException(
        409, "用户字段策略版本已改变", error_code="FIELD_POLICY_VERSION_CONFLICT"
    )


def _text(value: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or not value.isprintable()
    ):
        raise _invalid()
    return value.strip()


def _version(value: int, *, allow_zero: bool = True) -> None:
    if type(value) is not int or not (0 if allow_zero else 1) <= value < _MAX_VERSION:
        raise _invalid()


@dataclass(frozen=True, slots=True)
class PolicyStateSnapshot:
    tenant_id: str
    mode: str
    epoch: int
    catalog_digest: str
    candidate_manifest_digest: str | None
    active_manifest_digest: str | None
    ever_enforced: bool


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    tenant_id: str
    role_id: str
    revision_id: str
    revision_no: int
    version: int
    rules: tuple[FieldGrant, ...]
    rules_digest: str
    archived: bool
    created_at: datetime
    source_digest: str | None
    template_revision: str | None


@dataclass(frozen=True, slots=True)
class _WriteTarget:
    transaction: SessionTransaction
    tenant_id: int
    actor_user_id: int
    role_id: int | None


class UserFieldPolicyService:
    @staticmethod
    def _write_target(
        db: AsyncSession, tenant: TenantContext, role_id: int | None
    ) -> None:
        # G/B have already begun the outer transaction. Once actor is locked,
        # another method must not add a role lock later in that transaction.
        transaction = db.sync_session.get_transaction()
        assert transaction is not None
        previous = db.info.get(_WRITE_TARGET_KEY)
        if isinstance(previous, _WriteTarget) and previous.transaction is transaction:
            if (
                previous.tenant_id != tenant.tenant_id
                or previous.actor_user_id != tenant.actor_user_id
                or (role_id is not None and previous.role_id != role_id)
            ):
                raise lock_order_error()
        else:
            db.info[_WRITE_TARGET_KEY] = _WriteTarget(
                transaction, tenant.tenant_id, tenant.actor_user_id, role_id
            )

    @staticmethod
    async def _actor(db: AsyncSession, tenant: TenantContext, *, lock: bool) -> None:
        statement = select(User.user_id).where(
            User.tenant_id == tenant.tenant_id,
            User.user_id == tenant.actor_user_id,
            User.status == "1",
        )
        if lock:
            statement = statement.with_for_update()
        if await db.scalar(statement) is None:
            raise NotFoundException("用户")

    @staticmethod
    async def _role(
        db: AsyncSession, tenant: TenantContext, role_id: int, *, lock: bool
    ) -> None:
        _version(role_id, allow_zero=False)
        statement = select(Role.role_id).where(
            Role.tenant_id == tenant.tenant_id, Role.role_id == role_id
        )
        if lock:
            statement = statement.with_for_update()
        if await db.scalar(statement) is None:
            raise NotFoundException("角色")

    @staticmethod
    async def _state(db: AsyncSession, tenant: TenantContext) -> UserFieldPolicyState:
        state = await db.scalar(
            select(UserFieldPolicyState)
            .where(UserFieldPolicyState.tenant_id == tenant.tenant_id)
            .execution_options(populate_existing=True)
        )
        if state is None:
            raise unavailable()
        return state

    @staticmethod
    async def _manifest(
        db: AsyncSession, tenant: TenantContext
    ) -> tuple[str | None, int]:
        binding, revision = UserFieldPolicyBinding, UserFieldPolicyRevision
        expected_catalog = catalog_digest()
        rows = await db.stream(
            select(
                binding.role_id,
                binding.revision_id,
                binding.version,
                binding.archived_at,
                revision.rules_digest,
                revision.catalog_digest,
                revision.catalog_revision,
                revision.rules,
            )
            .join(
                revision,
                (revision.tenant_id == binding.tenant_id)
                & (revision.role_id == binding.role_id)
                & (revision.revision_id == binding.revision_id),
            )
            .where(binding.tenant_id == tenant.tenant_id)
            .order_by(binding.role_id)
            .limit(_MAX_BINDINGS + 1)
            .execution_options(yield_per=16)
        )
        items = []
        try:
            async for row in rows:
                if len(items) >= _MAX_BINDINGS:
                    raise _invalid()
                if (
                    row.catalog_digest != expected_catalog
                    or row.catalog_revision != CATALOG_REVISION
                ):
                    raise unavailable()
                try:
                    read_rules(row.rules, role_id=row.role_id)
                except InvalidParameterException:
                    raise unavailable() from None
                if rules_digest(row.rules) != row.rules_digest:
                    raise unavailable()
                items.append(
                    {
                        "role_id": str(row.role_id),
                        "revision_id": str(row.revision_id),
                        "version": row.version,
                        "archived": row.archived_at is not None,
                        "rules_digest": row.rules_digest,
                    }
                )
        finally:
            await rows.close()
        if not items:
            return None, 0
        return document_digest(
            {"catalog_digest": expected_catalog, "bindings": items}
        ), len(items)

    async def _check_manifest(
        self, db: AsyncSession, tenant: TenantContext, state: UserFieldPolicyState
    ) -> int:
        digest, count = await self._manifest(db, tenant)
        if digest != state.candidate_manifest_digest:
            raise unavailable()
        return count

    @staticmethod
    def _state_snapshot(state: UserFieldPolicyState) -> PolicyStateSnapshot:
        return PolicyStateSnapshot(
            str(state.tenant_id),
            state.mode,
            state.epoch,
            state.catalog_digest,
            state.candidate_manifest_digest,
            state.active_manifest_digest,
            state.ever_enforced,
        )

    @staticmethod
    async def _binding(
        db: AsyncSession, tenant: TenantContext, role_id: int
    ) -> UserFieldPolicyBinding | None:
        return await db.scalar(
            select(UserFieldPolicyBinding)
            .where(
                UserFieldPolicyBinding.tenant_id == tenant.tenant_id,
                UserFieldPolicyBinding.role_id == role_id,
            )
            .execution_options(populate_existing=True)
        )

    @staticmethod
    async def _snapshot(
        db: AsyncSession, tenant: TenantContext, binding: UserFieldPolicyBinding
    ) -> PolicySnapshot:
        revision = await db.scalar(
            select(UserFieldPolicyRevision)
            .where(
                UserFieldPolicyRevision.tenant_id == tenant.tenant_id,
                UserFieldPolicyRevision.role_id == binding.role_id,
                UserFieldPolicyRevision.revision_id == binding.revision_id,
            )
            .execution_options(populate_existing=True)
        )
        if (
            revision is None
            or revision.catalog_digest != catalog_digest()
            or revision.catalog_revision != CATALOG_REVISION
        ):
            raise unavailable()
        try:
            rules = read_rules(revision.rules, role_id=binding.role_id)
        except InvalidParameterException:
            raise unavailable() from None
        if rules_digest(revision.rules) != revision.rules_digest:
            raise unavailable()
        return PolicySnapshot(
            str(tenant.tenant_id),
            str(binding.role_id),
            str(revision.revision_id),
            revision.revision_no,
            binding.version,
            rules,
            revision.rules_digest,
            binding.archived_at is not None,
            revision.created_at,
            revision.source_digest,
            revision.template_revision,
        )

    async def get_state(
        self, db: AsyncSession, *, tenant: TenantContext
    ) -> PolicyStateSnapshot:
        require_tenant_context(tenant)
        await field_policy_barrier_service.acquire(db, tenants=(tenant,))
        await self._actor(db, tenant, lock=False)
        state = await self._state(db, tenant)
        await self._check_manifest(db, tenant, state)
        return self._state_snapshot(state)

    async def get_policy(
        self, db: AsyncSession, *, tenant: TenantContext, role_id: int
    ) -> PolicySnapshot | None:
        require_tenant_context(tenant)
        await field_policy_barrier_service.acquire(db, tenants=(tenant,))
        await self._role(db, tenant, role_id, lock=False)
        await self._actor(db, tenant, lock=False)
        state = await self._state(db, tenant)
        await self._check_manifest(db, tenant, state)
        binding = await self._binding(db, tenant, role_id)
        return None if binding is None else await self._snapshot(db, tenant, binding)

    async def _append_audit(self, db: AsyncSession, **values) -> None:
        db.add(UserFieldPolicyAudit(**values))
        await db.flush()

    async def publish(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        role_id: int,
        grants: tuple[FieldGrant, ...],
        expected_version: int,
        reason: str,
        correlation_id: str,
        source_digest: str | None = None,
        template_revision: str | None = None,
    ) -> PolicySnapshot:
        require_tenant_context(tenant)
        _version(expected_version)
        reason, correlation_id = _text(reason, 500), _text(correlation_id, 128)
        document = canonical_rules(grants, role_id=role_id)
        if (source_digest is None) != (template_revision is None):
            raise _invalid()
        if source_digest is not None:
            if (
                not isinstance(source_digest, str)
                or _DIGEST.fullmatch(source_digest) is None
            ):
                raise _invalid()
            template_revision = _text(template_revision, 64)
        await field_policy_barrier_service.acquire(
            db, tenants=(tenant,), mode="exclusive"
        )
        self._write_target(db, tenant, role_id)
        await self._role(db, tenant, role_id, lock=True)
        await self._actor(db, tenant, lock=True)
        state = await self._state(db, tenant)
        count = await self._check_manifest(db, tenant, state)
        binding = await self._binding(db, tenant, role_id)
        version = binding.version if binding is not None else 0
        if expected_version != version:
            raise _conflict()
        if binding is None and count >= _MAX_BINDINGS:
            raise _invalid()
        if binding is not None:
            await self._snapshot(db, tenant, binding)
        _version(state.epoch, allow_zero=False)
        before_digest = state.candidate_manifest_digest
        async with db.begin_nested():
            revision = UserFieldPolicyRevision(
                tenant_id=tenant.tenant_id,
                role_id=role_id,
                revision_no=version + 1,
                catalog_revision=CATALOG_REVISION,
                catalog_digest=catalog_digest(),
                rules=document,
                rules_digest=rules_digest(document),
                source_digest=source_digest,
                template_revision=template_revision,
                created_by_user_id=tenant.actor_user_id,
            )
            db.add(revision)
            await db.flush()
            if binding is None:
                binding = UserFieldPolicyBinding(
                    tenant_id=tenant.tenant_id,
                    role_id=role_id,
                    revision_id=revision.revision_id,
                    version=1,
                )
                db.add(binding)
            else:
                binding.revision_id = revision.revision_id
                binding.version += 1
                binding.archived_at = None
            await db.flush()
            manifest, _ = await self._manifest(db, tenant)
            state.epoch += 1
            state.mode = "legacy"
            state.candidate_manifest_digest = manifest
            await self._append_audit(
                db,
                tenant_id=tenant.tenant_id,
                role_id=role_id,
                actor_user_id=tenant.actor_user_id,
                action="publish",
                reason=reason,
                correlation_id=correlation_id,
                before_digest=before_digest,
                after_digest=state.candidate_manifest_digest,
                revision_id=revision.revision_id,
                before_version=version,
                after_version=binding.version,
                epoch=state.epoch,
            )
            return await self._snapshot(db, tenant, binding)

    async def archive(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        role_id: int,
        expected_version: int,
        reason: str,
        correlation_id: str,
    ) -> PolicySnapshot:
        require_tenant_context(tenant)
        _version(expected_version, allow_zero=False)
        reason, correlation_id = _text(reason, 500), _text(correlation_id, 128)
        await field_policy_barrier_service.acquire(
            db, tenants=(tenant,), mode="exclusive"
        )
        self._write_target(db, tenant, role_id)
        await self._role(db, tenant, role_id, lock=True)
        await self._actor(db, tenant, lock=True)
        state = await self._state(db, tenant)
        await self._check_manifest(db, tenant, state)
        binding = await self._binding(db, tenant, role_id)
        if (
            binding is None
            or binding.version != expected_version
            or binding.archived_at is not None
        ):
            raise _conflict()
        await self._snapshot(db, tenant, binding)
        _version(state.epoch, allow_zero=False)
        before_digest, version = state.candidate_manifest_digest, binding.version
        async with db.begin_nested():
            binding.version += 1
            binding.archived_at = datetime.now(UTC)
            await db.flush()
            manifest, _ = await self._manifest(db, tenant)
            state.epoch += 1
            state.mode = "legacy"
            state.candidate_manifest_digest = manifest
            await self._append_audit(
                db,
                tenant_id=tenant.tenant_id,
                role_id=role_id,
                actor_user_id=tenant.actor_user_id,
                action="archive",
                reason=reason,
                correlation_id=correlation_id,
                before_digest=before_digest,
                after_digest=state.candidate_manifest_digest,
                revision_id=binding.revision_id,
                before_version=version,
                after_version=binding.version,
                epoch=state.epoch,
            )
            return await self._snapshot(db, tenant, binding)

    async def mark_ready(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        expected_epoch: int,
        expected_manifest_digest: str,
        reason: str,
        correlation_id: str,
    ) -> PolicyStateSnapshot:
        require_tenant_context(tenant)
        _version(expected_epoch, allow_zero=False)
        reason, correlation_id = _text(reason, 500), _text(correlation_id, 128)
        await field_policy_barrier_service.acquire(
            db, tenants=(tenant,), mode="exclusive"
        )
        self._write_target(db, tenant, None)
        await self._actor(db, tenant, lock=True)
        state = await self._state(db, tenant)
        await self._check_manifest(db, tenant, state)
        if state.epoch != expected_epoch:
            raise _conflict()
        if (
            state.candidate_manifest_digest is None
            or state.candidate_manifest_digest != expected_manifest_digest
        ):
            raise BusinessException(
                409, "用户字段候选清单已改变", error_code="FIELD_POLICY_STALE"
            )
        async with db.begin_nested():
            state.mode = "ready"
            state.epoch += 1
            await self._append_audit(
                db,
                tenant_id=tenant.tenant_id,
                role_id=None,
                actor_user_id=tenant.actor_user_id,
                action="ready",
                reason=reason,
                correlation_id=correlation_id,
                before_digest=state.candidate_manifest_digest,
                after_digest=state.candidate_manifest_digest,
                revision_id=None,
                before_version=expected_epoch,
                after_version=state.epoch,
                epoch=state.epoch,
            )
            return self._state_snapshot(state)


user_field_policy_service = UserFieldPolicyService()
