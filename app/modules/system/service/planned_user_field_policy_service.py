"""Explicit policy candidates for planned owners; never a public auth boundary.

The caller supplies current management authority and owns commit/rollback. This
adapter neither upgrades an ordinary session nor invokes the legacy writer.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey, ResourceKind
from app.core.exceptions import (
    BusinessException,
    InvalidParameterException,
    NotFoundException,
)
from app.core.id_generator import next_id
from app.core.tenant_scope import require_tenant_context
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.base import role_depts, role_menus
from app.modules.system.field_policy.baseline import (
    RoleBaselineSource,
    build_role_baseline,
)
from app.modules.system.field_policy.documents import (
    canonical_rules,
    catalog_digest,
    document_digest,
    read_rules,
    rules_digest,
)
from app.modules.system.field_policy.user_fields import CATALOG_REVISION
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyBinding as Binding,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyRevision as Revision,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyState as State,
)
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.user_field_policy_service import (
    PolicySnapshot,
    PolicyStateSnapshot,
)

_MAX_BINDINGS = 1024
_MAX_VERSION = 2**63 - 1
_DIGEST = re.compile(r"[0-9a-f]{64}")
# Source discovery and protected revalidation share a conservative two-phase
# allowance. Historical manifest rules retain their separate 128 MiB profile.
_SOURCE_MAX_ROWS = 50_000
_SOURCE_MAX_BYTES = 32 * 1024 * 1024


def _error(code="FIELD_POLICY_LOCK_ORDER", status=409):
    return BusinessException(status, "字段策略事务或候选事实无效", error_code=code)


def _invalid():
    return InvalidParameterException(
        "字段策略候选输入无效", error_code="FIELD_POLICY_INVALID"
    )


def _text(value, maximum):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise _invalid()
    return value.strip()


def _version(value, *, zero=True):
    if type(value) is not int or not (0 if zero else 1) <= value < _MAX_VERSION:
        raise _invalid()


def _session(db, tx, tenant):
    require_tenant_context(tenant)
    if not isinstance(db, AuthorizationPlanSession):
        raise _error()
    tx.assert_session(db)


def _key(kind, tenant, identity):
    return ResourceKey(kind, tenant.tenant_id, identity)


@dataclass(frozen=True, slots=True)
class PolicyWritePlan:
    action: str
    tenant_id: int
    actor_user_id: int
    role_id: int | None
    lock_plan: LockPlan
    revision_id: int | None
    audit_id: int


async def assert_role_history_deletable(db, tx, *, tenant, role_ids):
    """Reject all targets before any deletion; other role rules belong to owner."""
    _session(db, tx, tenant)
    if not isinstance(role_ids, (tuple, list)) or not 1 <= len(role_ids) <= 256:
        raise _error("AUTHORIZATION_PLAN_LIMIT_EXCEEDED", 422)
    keys = tuple(_key(ResourceKind.ROLE, tenant, value) for value in role_ids)
    tx.assert_held(*keys)
    ids = tuple(sorted({key.identity[0] for key in keys}))
    for model in (Revision, Binding):
        if (
            await db.scalar(
                select(model.role_id)
                .where(model.tenant_id == tenant.tenant_id, model.role_id.in_(ids))
                .limit(1)
            )
            is not None
        ):
            raise _error("ROLE_DELETE_POLICY_HISTORY_REFERENCED")


class PlannedUserFieldPolicyService:
    @staticmethod
    async def _bindings(db, tenant):
        table = Binding.__table__
        rows, last = [], 0
        while True:
            page = (
                (
                    await db.execute(
                        select(table)
                        .where(
                            table.c.tenant_id == tenant.tenant_id,
                            table.c.role_id > last,
                        )
                        .order_by(table.c.role_id)
                        .limit(16)
                    )
                )
                .mappings()
                .all()
            )
            if not page:
                return rows
            rows.extend(page)
            if len(rows) > _MAX_BINDINGS:
                raise _invalid()
            last = page[-1]["role_id"]

    @staticmethod
    async def _source(db, tenant, role_id, budget):
        with bounded_authorization_reads(db, budget):
            return await PlannedUserFieldPolicyService._read_source(db, tenant, role_id)

    @staticmethod
    async def _read_source(db, tenant, role_id):
        role = (
            await db.execute(
                select(
                    Role.role_id, Role.role_code, Role.status, Role.data_scope
                ).where(Role.tenant_id == tenant.tenant_id, Role.role_id == role_id)
            )
        ).one_or_none()
        if role is None:
            raise NotFoundException("角色")
        links = (
            await db.execute(
                select(role_menus.c.menu_id, Menu.permission)
                .join(
                    Menu,
                    (Menu.tenant_id == role_menus.c.tenant_id)
                    & (Menu.menu_id == role_menus.c.menu_id),
                )
                .where(
                    role_menus.c.tenant_id == tenant.tenant_id,
                    role_menus.c.role_id == role_id,
                )
                .order_by(role_menus.c.menu_id)
                .limit(100001)
            )
        ).all()
        if len(links) > 100000:
            raise _error("AUTHORIZATION_PLAN_LIMIT_EXCEEDED", 422)
        departments = tuple(
            (
                await db.scalars(
                    select(role_depts.c.dept_id)
                    .where(
                        role_depts.c.tenant_id == tenant.tenant_id,
                        role_depts.c.role_id == role_id,
                    )
                    .order_by(role_depts.c.dept_id)
                    .limit(1025)
                )
            ).all()
        )
        permissions = frozenset(row.permission for row in links if row.permission)
        if len(departments) > 1024 or len(permissions) > 1024:
            raise _invalid()
        source = RoleBaselineSource(
            tenant.tenant_id,
            role_id,
            role.role_code,
            role.status,
            permissions,
            role.data_scope,
            departments,
        )
        keys = {_key(ResourceKind.ROLE, tenant, role_id)}
        for dept_id in departments:
            keys.add(_key(ResourceKind.DEPT, tenant, dept_id))
            keys.add(_key(ResourceKind.ROLE_DEPTS, tenant, (role_id, dept_id)))
        for row in links:
            keys.add(_key(ResourceKind.MENU, tenant, row.menu_id))
            keys.add(_key(ResourceKind.ROLE_MENUS, tenant, (role_id, row.menu_id)))
        return source, keys

    async def plan(self, db, tx, *, tenant, action, role_id=None):
        """Declare the entire candidate operation before the owner's one lock()."""
        _session(db, tx, tenant)
        if action not in {"publish", "archive", "ready"}:
            raise _invalid()
        if (action == "ready") != (role_id is None):
            raise _invalid()
        if role_id is not None:
            _version(role_id, zero=False)
        keys = {_key(ResourceKind.USER, tenant, tenant.actor_user_id)}
        bindings = await self._bindings(db, tenant)
        for binding in bindings:
            keys.add(_key(ResourceKind.POLICY_BINDING, tenant, binding["role_id"]))
        if role_id is not None:
            keys.add(_key(ResourceKind.ROLE, tenant, role_id))
        if action == "ready":
            source_budget = AuthorizationReadBudget(
                max_rows=_SOURCE_MAX_ROWS, max_bytes=_SOURCE_MAX_BYTES
            )
            for binding in bindings:
                if binding["archived_at"] is None:
                    _, dependencies = await self._source(
                        db, tenant, binding["role_id"], source_budget
                    )
                    keys.update(dependencies)
                    if len(keys) > 100000:
                        raise _error("AUTHORIZATION_PLAN_LIMIT_EXCEEDED", 422)
        audit_id = next_id()
        new = [NewResource(_key(ResourceKind.POLICY_AUDIT, tenant, audit_id))]
        revision_id = None
        if action == "publish":
            revision_id = next_id()
            new.append(
                NewResource(_key(ResourceKind.POLICY_REVISION, tenant, revision_id))
            )
            if not any(row["role_id"] == role_id for row in bindings):
                new.append(
                    NewResource(_key(ResourceKind.POLICY_BINDING, tenant, role_id))
                )
        return PolicyWritePlan(
            action,
            tenant.tenant_id,
            tenant.actor_user_id,
            role_id,
            LockPlan(existing=tuple(keys), new=tuple(new)),
            revision_id,
            audit_id,
        )

    @staticmethod
    async def _manifest(db, tx, tenant):
        binding, revision = Binding.__table__, Revision.__table__
        items, last = [], 0
        while True:
            rows = (
                await db.execute(
                    select(
                        binding.c.role_id,
                        binding.c.revision_id,
                        binding.c.version,
                        binding.c.archived_at,
                        revision.c.rules_digest,
                        revision.c.catalog_digest,
                        revision.c.catalog_revision,
                        revision.c.rules,
                    )
                    .outerjoin(
                        revision,
                        (revision.c.tenant_id == binding.c.tenant_id)
                        & (revision.c.role_id == binding.c.role_id)
                        & (revision.c.revision_id == binding.c.revision_id),
                    )
                    .where(
                        binding.c.tenant_id == tenant.tenant_id,
                        binding.c.role_id > last,
                    )
                    .order_by(binding.c.role_id)
                    .limit(16)
                )
            ).all()
            if not rows:
                break
            for row in rows:
                tx.assert_held(_key(ResourceKind.POLICY_BINDING, tenant, row.role_id))
                if len(items) >= _MAX_BINDINGS:
                    raise _invalid()
                if (
                    row.catalog_digest != catalog_digest()
                    or row.catalog_revision != CATALOG_REVISION
                ):
                    raise _error("FIELD_POLICY_UNAVAILABLE", 503)
                try:
                    read_rules(row.rules, role_id=row.role_id)
                    if rules_digest(row.rules) != row.rules_digest:
                        raise _invalid()
                except InvalidParameterException:
                    raise _error("FIELD_POLICY_UNAVAILABLE", 503) from None
                items.append(
                    {
                        "role_id": str(row.role_id),
                        "revision_id": str(row.revision_id),
                        "version": row.version,
                        "archived": row.archived_at is not None,
                        "rules_digest": row.rules_digest,
                    }
                )
            last = rows[-1].role_id
        return (
            document_digest({"catalog_digest": catalog_digest(), "bindings": items})
            if items
            else None
        ), len(items)

    @staticmethod
    async def _state(db, tenant):
        row = (
            (
                await db.execute(
                    select(State.__table__).where(State.tenant_id == tenant.tenant_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise _error("FIELD_POLICY_UNAVAILABLE", 503)
        return row

    @staticmethod
    def _state_snapshot(row):
        return PolicyStateSnapshot(
            str(row["tenant_id"]),
            row["mode"],
            row["epoch"],
            row["catalog_digest"],
            row["candidate_manifest_digest"],
            row["active_manifest_digest"],
            row["ever_enforced"],
        )

    @staticmethod
    async def _binding(db, tenant, role_id):
        return (
            (
                await db.execute(
                    select(Binding.__table__).where(
                        Binding.tenant_id == tenant.tenant_id,
                        Binding.role_id == role_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    async def _revision(db, tenant, binding):
        row = (
            (
                await db.execute(
                    select(Revision.__table__).where(
                        Revision.tenant_id == tenant.tenant_id,
                        Revision.role_id == binding["role_id"],
                        Revision.revision_id == binding["revision_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or row["catalog_digest"] != catalog_digest()
            or row["catalog_revision"] != CATALOG_REVISION
        ):
            raise _error("FIELD_POLICY_UNAVAILABLE", 503)
        return row

    async def _snapshot(self, db, tenant, binding):
        row = await self._revision(db, tenant, binding)
        rules = read_rules(row["rules"], role_id=binding["role_id"])
        if rules_digest(row["rules"]) != row["rules_digest"]:
            raise _error("FIELD_POLICY_UNAVAILABLE", 503)
        return PolicySnapshot(
            str(tenant.tenant_id),
            str(binding["role_id"]),
            str(row["revision_id"]),
            row["revision_no"],
            binding["version"],
            rules,
            row["rules_digest"],
            binding["archived_at"] is not None,
            row["created_at"],
            row["source_digest"],
            row["template_revision"],
        )

    async def _prepare(self, db, tx, plan, tenant, action, reason, correlation_id):
        _session(db, tx, tenant)
        if type(plan) is not PolicyWritePlan or (
            plan.action,
            plan.tenant_id,
            plan.actor_user_id,
        ) != (action, tenant.tenant_id, tenant.actor_user_id):
            raise _error()
        if (action == "ready") != (plan.role_id is None):
            raise _error()
        # A declaration can be rebuilt by callers. Only actual executor-held
        # keys establish proof, including mandatory keys omitted from a plan.
        tx.assert_held(_key(ResourceKind.USER, tenant, tenant.actor_user_id))
        if plan.role_id is not None:
            tx.assert_held(_key(ResourceKind.ROLE, tenant, plan.role_id))
        tx.assert_held(*plan.lock_plan.existing)
        if (
            await db.scalar(
                select(User.user_id).where(
                    User.tenant_id == tenant.tenant_id,
                    User.user_id == tenant.actor_user_id,
                    User.status == "1",
                )
            )
            is None
        ):
            raise NotFoundException("用户")
        reason, correlation_id = _text(reason, 500), _text(correlation_id, 128)
        state = await self._state(db, tenant)
        digest, count = await self._manifest(db, tx, tenant)
        if digest != state["candidate_manifest_digest"]:
            raise _error("FIELD_POLICY_UNAVAILABLE", 503)
        return state, count, reason, correlation_id

    @staticmethod
    async def _audit(
        tx,
        plan,
        tenant,
        *,
        action,
        state,
        manifest,
        epoch,
        revision_id,
        before_version,
        after_version,
        reason,
        correlation_id,
    ):
        await tx.insert_reserved(
            _key(ResourceKind.POLICY_AUDIT, tenant, plan.audit_id),
            {
                "role_id": plan.role_id,
                "actor_user_id": tenant.actor_user_id,
                "action": action,
                "reason": reason,
                "correlation_id": correlation_id,
                "before_digest": state["candidate_manifest_digest"],
                "after_digest": manifest,
                "revision_id": revision_id,
                "before_version": before_version,
                "after_version": after_version,
                "epoch": epoch,
            },
        )

    async def publish(
        self,
        db,
        tx,
        plan,
        *,
        tenant,
        grants,
        expected_version,
        reason,
        correlation_id,
        source_digest=None,
        template_revision=None,
    ):
        _version(expected_version)
        if (source_digest is None) != (template_revision is None):
            raise _invalid()
        if source_digest is not None:
            if (
                not isinstance(source_digest, str)
                or _DIGEST.fullmatch(source_digest) is None
            ):
                raise _invalid()
            template_revision = _text(template_revision, 64)
        state, count, reason, correlation_id = await self._prepare(
            db, tx, plan, tenant, "publish", reason, correlation_id
        )
        document = canonical_rules(grants, role_id=plan.role_id)
        binding = await self._binding(db, tenant, plan.role_id)
        before = binding["version"] if binding is not None else 0
        if before != expected_version:
            raise _error("FIELD_POLICY_VERSION_CONFLICT")
        if binding is None and count >= _MAX_BINDINGS:
            raise _invalid()
        _version(state["epoch"], zero=False)
        async with tx.savepoint():
            async with tx.policy_change(tenant.tenant_id, action="publish"):
                await tx.insert_reserved(
                    _key(ResourceKind.POLICY_REVISION, tenant, plan.revision_id),
                    {
                        "role_id": plan.role_id,
                        "revision_no": before + 1,
                        "catalog_revision": CATALOG_REVISION,
                        "catalog_digest": catalog_digest(),
                        "rules": document,
                        "rules_digest": rules_digest(document),
                        "source_digest": source_digest,
                        "template_revision": template_revision,
                        "created_by_user_id": tenant.actor_user_id,
                    },
                )
                key = _key(ResourceKind.POLICY_BINDING, tenant, plan.role_id)
                if binding is None:
                    await tx.insert_reserved(
                        key, {"revision_id": plan.revision_id, "version": 1}
                    )
                else:
                    await tx.update_held(
                        key,
                        {
                            "revision_id": plan.revision_id,
                            "version": before + 1,
                            "archived_at": None,
                        },
                        expected={"version": before},
                    )
                manifest, _ = await self._manifest(db, tx, tenant)
                epoch = await tx.complete_policy_change(
                    expected_epoch=state["epoch"],
                    expected_manifest=state["candidate_manifest_digest"],
                    manifest_digest=manifest,
                )
                await self._audit(
                    tx,
                    plan,
                    tenant,
                    action="publish",
                    state=state,
                    manifest=manifest,
                    epoch=epoch,
                    revision_id=plan.revision_id,
                    before_version=before,
                    after_version=before + 1,
                    reason=reason,
                    correlation_id=correlation_id,
                )
        return await self._snapshot(
            db, tenant, await self._binding(db, tenant, plan.role_id)
        )

    async def archive(
        self, db, tx, plan, *, tenant, expected_version, reason, correlation_id
    ):
        _version(expected_version, zero=False)
        state, _, reason, correlation_id = await self._prepare(
            db, tx, plan, tenant, "archive", reason, correlation_id
        )
        binding = await self._binding(db, tenant, plan.role_id)
        if (
            binding is None
            or binding["version"] != expected_version
            or binding["archived_at"] is not None
        ):
            raise _error("FIELD_POLICY_VERSION_CONFLICT")
        async with tx.savepoint():
            async with tx.policy_change(tenant.tenant_id, action="archive"):
                await tx.update_held(
                    _key(ResourceKind.POLICY_BINDING, tenant, plan.role_id),
                    {"version": expected_version + 1, "archived_at": datetime.now(UTC)},
                    expected={"version": expected_version},
                )
                manifest, _ = await self._manifest(db, tx, tenant)
                epoch = await tx.complete_policy_change(
                    expected_epoch=state["epoch"],
                    expected_manifest=state["candidate_manifest_digest"],
                    manifest_digest=manifest,
                )
                await self._audit(
                    tx,
                    plan,
                    tenant,
                    action="archive",
                    state=state,
                    manifest=manifest,
                    epoch=epoch,
                    revision_id=binding["revision_id"],
                    before_version=expected_version,
                    after_version=expected_version + 1,
                    reason=reason,
                    correlation_id=correlation_id,
                )
        return await self._snapshot(
            db, tenant, await self._binding(db, tenant, plan.role_id)
        )

    async def mark_ready(
        self,
        db,
        tx,
        plan,
        *,
        tenant,
        expected_epoch,
        expected_manifest_digest,
        reason,
        correlation_id,
    ):
        _version(expected_epoch, zero=False)
        state, _, reason, correlation_id = await self._prepare(
            db, tx, plan, tenant, "ready", reason, correlation_id
        )
        if state["epoch"] != expected_epoch:
            raise _error("FIELD_POLICY_VERSION_CONFLICT")
        manifest = state["candidate_manifest_digest"]
        if manifest is None or manifest != expected_manifest_digest:
            raise _error("FIELD_POLICY_STALE")
        source_budget = AuthorizationReadBudget(
            max_rows=_SOURCE_MAX_ROWS, max_bytes=_SOURCE_MAX_BYTES
        )
        for binding in await self._bindings(db, tenant):
            if binding["archived_at"] is not None:
                continue
            source, dependencies = await self._source(
                db, tenant, binding["role_id"], source_budget
            )
            tx.assert_held(*dependencies)
            revision = await self._revision(db, tenant, binding)
            if (
                revision["template_revision"] is None
                and revision["source_digest"] is None
            ):
                continue
            if revision["template_revision"] != "legacy-user-fields/v1":
                raise _error("FIELD_POLICY_STALE")
            try:
                actual = build_role_baseline(source).source_digest
            except InvalidParameterException:
                raise _error("FIELD_POLICY_STALE") from None
            if actual != revision["source_digest"]:
                raise _error("FIELD_POLICY_STALE")
        if state["mode"] == "ready":
            return self._state_snapshot(state)
        async with tx.savepoint():
            async with tx.policy_change(tenant.tenant_id, action="ready"):
                epoch = await tx.complete_policy_change(
                    expected_epoch=expected_epoch,
                    expected_manifest=manifest,
                    manifest_digest=manifest,
                )
                await self._audit(
                    tx,
                    plan,
                    tenant,
                    action="ready",
                    state=state,
                    manifest=manifest,
                    epoch=epoch,
                    revision_id=None,
                    before_version=expected_epoch,
                    after_version=epoch,
                    reason=reason,
                    correlation_id=correlation_id,
                )
        return self._state_snapshot(await self._state(db, tenant))


planned_user_field_policy_service = PlannedUserFieldPolicyService()
