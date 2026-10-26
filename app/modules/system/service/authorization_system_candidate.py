"""Explicit candidate composition for System mutations; no production fallback.

The owner passes a token's separate user security version and commits after this
service returns. Existing route and AI owner composition stays unchanged until
the complete writer migration is qualified.
"""

import asyncio
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.orm import noload

from app.constants import ADMIN_USERNAME, SUPER_ADMIN_ROLE_CODE
from app.core.authorization_io import hash_password, verify_password
from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import (
    AuthenticationException,
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.id_generator import next_id
from app.core.tenant import TenantContext
from app.db.authorization_owner import rollback_authorization_session
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptCreate, DeptMove, DeptUpdate
from app.modules.system.schemas.role import RoleCreate, RoleUpdate
from app.modules.system.schemas.user import (
    ChangePassword,
    ResetPassword,
    UpdateProfile,
    UserCreate,
    UserUpdate,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_resource_catalog import (
    resource_spec,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.dept_service import dept_service
from app.modules.system.service.grant_authority import grant_authority_service
from app.modules.system.service.planned_user_field_policy_service import (
    assert_role_history_deletable,
)
from app.modules.system.service.role_management_service import role_management_service
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service as departments,
)
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service as roles,
)


@dataclass(frozen=True)
class CandidateEntry:
    permissions: tuple[str, ...]
    source: str
    mode: str = "exclusive"
    epoch_owner: str = "system_aggregate"
    production_connected: bool = False


SYSTEM_CANDIDATE_ENTRIES = MappingProxyType(
    {
        "user.create": CandidateEntry(
            ("system:user:add",), "UserOperationsService.create_user"
        ),
        "user.update": CandidateEntry(("system:user:edit",), "UserService.update_user"),
        "user.profile": CandidateEntry((), "UserService.update_profile"),
        "user.password": CandidateEntry(
            ("system:user:reset-password",), "UserService.reset_password"
        ),
        "user.change_password": CandidateEntry((), "UserService.change_password"),
        "user.revoke": CandidateEntry(
            ("system:user:reset-password",), "UserService.revoke_sessions"
        ),
        "user.roles": CandidateEntry(
            ("system:user:edit", "system:user:role-auth"),
            "UserRoleAssignmentService.replace_roles",
        ),
        "user.departments": CandidateEntry(
            ("system:user:edit", "system:dept:list"),
            "UserDepartmentAssignmentService.replace_departments",
        ),
        "user.delete": CandidateEntry(
            ("system:user:delete",), "UserService.delete_user"
        ),
        "user.batch_delete": CandidateEntry(
            ("system:user:batch-delete",), "UserService.batch_delete_users"
        ),
        "role.create": CandidateEntry(
            ("system:role:add",), "RoleManagementService.create"
        ),
        "role.update": CandidateEntry(
            ("system:role:edit",), "RoleManagementService.update"
        ),
        "role.menus": CandidateEntry(
            ("system:role:menu-auth",), "RoleManagementService.update_menus"
        ),
        "role.delete": CandidateEntry(
            ("system:role:delete",), "RoleService.delete_role"
        ),
        "role.batch_delete": CandidateEntry(
            ("system:role:batch-delete",), "RoleService.batch_delete_roles"
        ),
        "dept.create": CandidateEntry(
            ("system:dept:add", "system:dept:list"), "DeptService.create"
        ),
        "dept.update": CandidateEntry(
            ("system:dept:edit", "system:dept:list"), "DeptService.update"
        ),
        "dept.move": CandidateEntry(
            ("system:dept:move", "system:dept:list"), "DeptService.move"
        ),
        "dept.delete": CandidateEntry(("system:dept:delete",), "DeptService.delete"),
        "dept.batch_delete": CandidateEntry(
            ("system:dept:batch-delete",), "DeptService.batch_delete"
        ),
    }
)

_KINDS = (
    ResourceKind.ROLE,
    ResourceKind.DEPT,
    ResourceKind.USER,
    ResourceKind.MENU,
    ResourceKind.ROLE_AGENT,
    ResourceKind.SETTING,
    ResourceKind.USER_ROLES,
    ResourceKind.USER_DEPTS,
    ResourceKind.ROLE_DEPTS,
    ResourceKind.ROLE_MENUS,
)
_CAPS = {ResourceKind.ROLE: 4096, ResourceKind.DEPT: 8192, ResourceKind.USER: 10000}


def _limit():
    return BusinessException(
        422,
        "授权输入或影响集合超过上限",
        error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
    )


def _ids(values):
    if not isinstance(values, (list, tuple)) or len(values) > 256:
        raise _limit()
    result = []
    for value in values:
        if (
            isinstance(value, bool)
            or not str(value).isascii()
            or not str(value).isdigit()
            or not 0 < int(value) < 2**63
        ):
            raise _order_error()
        result.append(int(value))
    return tuple(sorted(set(result)))


def _key(kind, tenant_id, *identity):
    return ResourceKey(kind, tenant_id, identity)


def _expect(actual, expected):
    if expected is not None and actual != expected:
        raise BusinessRuleException(
            "授权审批快照已变化", error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE"
        )


class SystemAuthorizationCandidate:
    async def _facts(self, db, tenant_id):
        facts = {}
        for kind in _KINDS:
            spec = resource_spec(kind)
            cap = _CAPS.get(kind, 100000)
            rows = (
                (
                    await db.execute(
                        select(spec.table)
                        .where(spec.table.c[spec.tenant_column] == tenant_id)
                        .order_by(*(spec.table.c[name] for name in spec.key_columns))
                        .limit(cap + 1)
                    )
                )
                .mappings()
                .all()
            )
            if len(rows) > cap:
                raise _limit()
            facts[kind] = [dict(row) for row in rows]
        return facts

    async def _identity(self, db, tenant, user_version):
        if type(user_version) is not int or user_version < 1:
            raise _order_error()
        live_tenant = await db.scalar(
            select(Tenant)
            .where(Tenant.tenant_id == tenant.tenant_id)
            .execution_options(populate_existing=True)
        )
        actor = await db.scalar(
            select(User)
            .where(
                User.tenant_id == tenant.tenant_id, User.user_id == tenant.actor_user_id
            )
            .options(noload("*"))
            .execution_options(populate_existing=True)
        )
        if (
            live_tenant is None
            or live_tenant.status != "1"
            or live_tenant.row_version != tenant.tenant_version
            or live_tenant.tenant_code != tenant.tenant_code
            or actor is None
            or actor.status != "1"
            or actor.auth_version != user_version
        ):
            raise AuthenticationException(
                "当前身份已经失效", error_code="INVALID_CREDENTIALS"
            )
        return actor

    def _check_input(self, operation, payload, target_id):
        expected = {
            "user.create": UserCreate,
            "user.update": UserUpdate,
            "user.profile": UpdateProfile,
            "user.password": ResetPassword,
            "user.change_password": ChangePassword,
            "role.create": RoleCreate,
            "role.update": RoleUpdate,
            "dept.create": DeptCreate,
            "dept.update": DeptUpdate,
            "dept.move": DeptMove,
        }.get(operation)
        if expected and type(payload) is not expected:
            raise _order_error()
        if target_id is not None:
            _ids([target_id])
        if operation in {
            "user.roles",
            "role.menus",
            "user.batch_delete",
            "role.batch_delete",
            "dept.batch_delete",
        }:
            _ids(payload)
        if operation == "user.departments":
            if not isinstance(payload, (list, tuple)) or len(payload) > 256:
                raise _limit()
            departments._normalize_assignments(payload)
        for name in ("role_ids", "dept_ids"):
            values = getattr(payload, name, None)
            if values is not None and len(values) > 256:
                raise _limit()

    async def execute(
        self,
        db,
        *,
        tenant: TenantContext,
        actor_user_version: int,
        operation: str,
        payload=None,
        target_id=None,
        expected_snapshot=None,
    ):
        if (
            not isinstance(db, AuthorizationPlanSession)
            or type(tenant) is not TenantContext
            or operation not in SYSTEM_CANDIDATE_ENTRIES
        ):
            raise _order_error()
        self._check_input(operation, payload, target_id)
        if (
            db.in_transaction()
            or db.new
            or db.dirty
            or db.deleted
            or db._authorization_journal.unsafe
            or db._authorization_journal.phase != "idle"
        ):
            raise _order_error()
        # A caller cannot mutate a submitted model/list during external preparation.
        if hasattr(payload, "model_dump"):
            submitted = payload.model_dump(exclude_unset=True)
            # DeptMove validates canonical strings, then stores the ID as int.
            # Restore only that exact internal type for strict revalidation.
            if (
                type(payload) is DeptMove
                and type(submitted.get("new_parent_id")) is int
            ):
                submitted["new_parent_id"] = str(submitted["new_parent_id"])
            payload = type(payload).model_validate(submitted)
        elif isinstance(payload, (list, tuple)):
            payload = tuple(
                tuple(item) if isinstance(item, (list, tuple)) else item
                for item in payload
            )
        if target_id is not None:
            target_id = int(target_id)
        password_hash = None
        old_password_facts = None
        if operation == "user.create":
            password_hash = await hash_password(payload.password)
        elif operation in {"user.password", "user.change_password"}:
            if operation == "user.change_password":
                target_id = tenant.actor_user_id
                try:
                    async with asyncio.timeout(5):
                        old_password_facts = (
                            await db.execute(
                                select(User.hashed_password, User.auth_version).where(
                                    User.tenant_id == tenant.tenant_id,
                                    User.user_id == target_id,
                                )
                            )
                        ).one_or_none()
                except BaseException as exc:
                    try:
                        await rollback_authorization_session(db)
                    except Exception:
                        if isinstance(exc, asyncio.CancelledError):
                            raise exc
                        raise
                    raise
                else:
                    await rollback_authorization_session(db)
                if old_password_facts is None or not await verify_password(
                    payload.old_password, old_password_facts.hashed_password
                ):
                    raise BusinessRuleException(
                        "当前密码不正确", error_code="INCORRECT_OLD_PASSWORD"
                    )
            password_hash = await hash_password(payload.new_password)
        if operation == "user.profile":
            target_id = tenant.actor_user_id
        new_id = next_id() if operation.endswith(".create") else None
        with bounded_authorization_reads(db) as io:
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),)
            ) as tx:
                await self._identity(db, tenant, actor_user_version)
                facts = await self._facts(db, tenant.tenant_id)
                authority = await grant_authority_service.build(
                    db, tenant.actor_user_id, tenant=tenant
                )
                entry = SYSTEM_CANDIDATE_ENTRIES[operation]
                if not set(entry.permissions) <= authority.permission_codes:
                    raise AuthorizationException(
                        "缺少操作权限", error_code="MISSING_PERMISSION"
                    )
                prepared = await self._prepare(
                    db,
                    tenant,
                    operation,
                    payload,
                    target_id,
                    new_id,
                    facts,
                    authority,
                    expected_snapshot,
                )
                existing = tuple(
                    _key(
                        kind,
                        tenant.tenant_id,
                        *(row[name] for name in resource_spec(kind).key_columns),
                    )
                    for kind, rows in facts.items()
                    for row in rows
                )
                new = tuple(
                    NewResource(key) for key in prepared["new"] if key not in existing
                )
                await tx.lock(LockPlan(existing=existing, new=new))
                await self._identity(db, tenant, actor_user_version)
                current = await self._facts(db, tenant.tenant_id)
                if current != facts:
                    raise BusinessRuleException(
                        "授权事实已变化", error_code="AUTHORIZATION_SNAPSHOT_STALE"
                    )
                async with tx.aggregate(tenant.tenant_id):
                    result = await self._apply(
                        db,
                        tx,
                        tenant,
                        operation,
                        payload,
                        target_id,
                        new_id,
                        prepared,
                        facts,
                        authority,
                        password_hash,
                        old_password_facts,
                    )
        return {
            "id": new_id or target_id,
            "result": result,
            "io": {
                "statements": io.statements,
                "rows": io.rows,
                "decoded_bytes": io.decoded_bytes,
            },
        }

    async def _prepare(
        self,
        db,
        tenant,
        operation,
        payload,
        target_id,
        new_id,
        facts,
        authority,
        expected,
    ):
        tid = tenant.tenant_id
        result = {"new": set()}
        if operation.startswith("role.") and operation in {
            "role.create",
            "role.update",
            "role.menus",
        }:
            action = {
                "role.create": "create",
                "role.update": "update",
                "role.menus": "update_menus",
            }[operation]
            evaluation = await role_management_service._evaluate(
                db,
                action=action,
                actor_user_id=tenant.actor_user_id,
                tenant=tenant,
                role_in=payload if operation != "role.menus" else None,
                role_id=target_id,
                menu_ids=list(_ids(payload)) if operation == "role.menus" else None,
            )
            _expect(evaluation.preview.snapshot, expected)
            result["evaluation"] = evaluation
            rid = new_id or target_id
            if new_id:
                result["new"].add(_key(ResourceKind.ROLE, tid, rid))
            for dept in evaluation.candidate_depts:
                result["new"].add(
                    _key(ResourceKind.ROLE_DEPTS, tid, rid, int(dept.dept_id))
                )
            for menu in evaluation.candidate_menus:
                result["new"].add(
                    _key(ResourceKind.ROLE_MENUS, tid, rid, int(menu.menu_id))
                )
        elif operation.startswith("dept.") and operation in {
            "dept.create",
            "dept.update",
            "dept.move",
        }:
            preview, *_ = await dept_service._build_preview(
                db,
                tenant=tenant,
                action=operation.split(".")[1],
                actor_user_id=tenant.actor_user_id,
                dept_in=payload if operation != "dept.move" else None,
                dept_id=target_id,
                new_parent_id=payload.new_parent_id
                if operation == "dept.move"
                else None,
            )
            _expect(preview.snapshot, expected)
            if new_id:
                result["new"].add(_key(ResourceKind.DEPT, tid, new_id))
        elif operation == "user.create":
            if any(
                row["user_name"] == payload.user_name
                for row in facts[ResourceKind.USER]
            ):
                raise DuplicateException("用户名", payload.user_name)
            await roles.ensure_create_permissions(
                db,
                actor_user_id=tenant.actor_user_id,
                explicit_roles=payload.role_ids is not None,
                tenant=tenant,
            )
            await departments.ensure_create_permissions(
                db,
                actor_user_id=tenant.actor_user_id,
                has_departments=bool(payload.dept_ids),
                tenant=tenant,
            )
            requested = (
                await roles._load_requested_roles(db, payload.role_ids, tenant=tenant)
                if payload.role_ids is not None
                else [await roles._default_role(db, tenant=tenant)]
            )
            assignments = departments._normalize_assignments(
                [(item.dept_id, item.is_primary) for item in payload.dept_ids]
            )
            target = User(
                user_id=new_id,
                tenant_id=tid,
                user_name=payload.user_name,
                status=payload.status,
                roles=[],
                depts=[],
            )
            depts = await departments._load_requested_depts(
                db, assignments, tenant=tenant
            )
            await roles._authorize_role_set(
                db,
                actor=authority,
                actor_user_id=tenant.actor_user_id,
                target=target,
                requested_roles=requested,
                prospective_depts=depts,
                allow_empty_old_roles=True,
                created_target=True,
                tenant=tenant,
            )
            await departments._authorize_assignment_change(
                db,
                authority=authority,
                target=target,
                old_assignments=(),
                new_assignments=assignments,
                requested_depts=depts,
                prospective_roles=requested,
                created_target=True,
                require_primary=self._primary(facts),
                tenant=tenant,
            )
            result.update(
                roles=tuple(int(role.role_id) for role in requested),
                assignments=assignments,
            )
            result["new"].add(_key(ResourceKind.USER, tid, new_id))
            result["new"].update(
                _key(ResourceKind.USER_ROLES, tid, new_id, rid)
                for rid in result["roles"]
            )
            result["new"].update(
                _key(ResourceKind.USER_DEPTS, tid, new_id, did)
                for did, _ in assignments
            )
        elif operation == "user.roles":
            preview = await roles.preview_roles(
                db,
                actor_user_id=tenant.actor_user_id,
                target_user_id=target_id,
                role_ids=list(_ids(payload)),
                tenant=tenant,
            )
            _expect(preview.snapshot, expected)
            result["roles"] = preview.new_role_ids
            result["new"].update(
                _key(ResourceKind.USER_ROLES, tid, target_id, rid)
                for rid in preview.new_role_ids
            )
        elif operation == "user.departments":
            assignments = departments._normalize_assignments(payload)
            target = await departments._load_user(db, target_id, tenant=tenant)
            old = await departments._load_assignments(db, target_id, tenant=tenant)
            requested = await departments._load_requested_depts(
                db, assignments, tenant=tenant
            )
            before, after = await departments._authorize_assignment_change(
                db,
                authority=authority,
                target=target,
                old_assignments=old,
                new_assignments=assignments,
                requested_depts=requested,
                require_primary=self._primary(facts),
                tenant=tenant,
            )
            snapshot = departments._build_replacement_snapshot(
                authority=authority,
                target=target,
                old_assignments=old,
                new_assignments=assignments,
                old_authority=before,
                new_authority=after,
                require_primary=self._primary(facts),
                dept_map={int(d.dept_id): d for d in [*target.depts, *requested]},
            )
            _expect(snapshot, expected)
            result["assignments"] = assignments
            result["new"].update(
                _key(ResourceKind.USER_DEPTS, tid, target_id, did)
                for did, _ in assignments
            )
        return result

    @staticmethod
    def _primary(facts):
        return any(
            row["setting_key"] == "user_require_primary_dept"
            and row["status"] == "1"
            and row["setting_value"].strip().lower() in {"true", "1", "yes", "on"}
            for row in facts[ResourceKind.SETTING]
        )

    async def _replace(self, tx, tid, kind, parent_id, wanted, facts):
        spec = resource_spec(kind)
        current = {
            row[spec.key_columns[1]]: row
            for row in facts[kind]
            if row[spec.key_columns[0]] == parent_id
        }
        for child_id in sorted(current.keys() - wanted.keys()):
            await tx.delete_held(_key(kind, tid, parent_id, child_id))
        # Clear old primary first so the existing unique primary index remains valid.
        if kind == ResourceKind.USER_DEPTS:
            for child_id, row in sorted(current.items()):
                if (
                    child_id in wanted
                    and row["is_primary"] == "Y"
                    and wanted[child_id].get("is_primary") != "Y"
                ):
                    await tx.update_held(
                        _key(kind, tid, parent_id, child_id), wanted[child_id]
                    )
        for child_id, values in sorted(wanted.items()):
            key = _key(kind, tid, parent_id, child_id)
            if child_id not in current:
                await tx.insert_reserved(key, values)
            elif values:
                await tx.update_held(key, values)

    async def _apply(
        self,
        db,
        tx,
        tenant,
        operation,
        payload,
        target_id,
        new_id,
        prepared,
        facts,
        authority,
        password_hash,
        old_password_facts,
    ):
        tid = tenant.tenant_id
        if operation == "user.create":
            values = payload.model_dump(
                exclude={"password", "role_ids", "dept_ids"}
            ) | {"hashed_password": password_hash}
            await tx.insert_reserved(_key(ResourceKind.USER, tid, new_id), values)
            await self._replace(
                tx,
                tid,
                ResourceKind.USER_ROLES,
                new_id,
                {rid: {} for rid in prepared["roles"]},
                facts,
            )
            await self._replace(
                tx,
                tid,
                ResourceKind.USER_DEPTS,
                new_id,
                {
                    did: {"is_primary": "Y" if primary else "N"}
                    for did, primary in prepared["assignments"]
                },
                facts,
            )
            return True
        if operation == "user.roles":
            return await self._replace(
                tx,
                tid,
                ResourceKind.USER_ROLES,
                target_id,
                {rid: {} for rid in prepared["roles"]},
                facts,
            )
        if operation == "user.departments":
            return await self._replace(
                tx,
                tid,
                ResourceKind.USER_DEPTS,
                target_id,
                {
                    did: {"is_primary": "Y" if primary else "N"}
                    for did, primary in prepared["assignments"]
                },
                facts,
            )
        if operation in {
            "user.update",
            "user.profile",
            "user.password",
            "user.change_password",
            "user.revoke",
        }:
            row = next(
                (r for r in facts[ResourceKind.USER] if r["user_id"] == target_id), None
            )
            if row is None:
                raise NotFoundException("用户")
            roles._ensure_target_visible(authority, target_id)
            if old_password_facts is not None and (
                row["hashed_password"],
                row["auth_version"],
            ) != tuple(old_password_facts):
                raise AuthenticationException(
                    "账户安全状态已变化", error_code="INVALID_CREDENTIALS"
                )
            if operation in {"user.password", "user.change_password", "user.revoke"}:
                values = {"auth_version": row["auth_version"] + 1}
                if password_hash:
                    values["hashed_password"] = password_hash
            else:
                values = payload.model_dump(exclude_unset=True)
                if "status" in values and values["status"] != row["status"]:
                    values["auth_version"] = row["auth_version"] + 1
                if "user_email" in values and values["user_email"] != row["user_email"]:
                    values["email_verified_at"] = None
            return await tx.update_held(_key(ResourceKind.USER, tid, target_id), values)
        if operation in {"role.create", "role.update", "role.menus"}:
            evaluation = prepared["evaluation"]
            rid = new_id or target_id
            if new_id:
                await tx.insert_reserved(
                    _key(ResourceKind.ROLE, tid, rid),
                    payload.model_dump(exclude={"dept_ids"}),
                )
            elif operation == "role.update":
                values = payload.model_dump(exclude_unset=True, exclude={"dept_ids"})
                row = next(r for r in facts[ResourceKind.ROLE] if r["role_id"] == rid)
                translations = dict(row["i18n_keys"] or {})
                for name, camel in (
                    ("role_name", "roleName"),
                    ("role_desc", "roleDesc"),
                ):
                    if name in values and values[name] != row[name]:
                        translations.pop(camel, None)
                if translations != (row["i18n_keys"] or {}):
                    values["i18n_keys"] = translations
                await tx.update_held(_key(ResourceKind.ROLE, tid, rid), values)
            if operation == "role.menus":
                await self._replace(
                    tx,
                    tid,
                    ResourceKind.ROLE_MENUS,
                    rid,
                    {int(m.menu_id): {} for m in evaluation.candidate_menus},
                    facts,
                )
            elif new_id or {"dept_ids", "data_scope"} & payload.model_fields_set:
                await self._replace(
                    tx,
                    tid,
                    ResourceKind.ROLE_DEPTS,
                    rid,
                    {int(d.dept_id): {} for d in evaluation.candidate_depts},
                    facts,
                )
            return True
        if operation in {"dept.create", "dept.update", "dept.move"}:
            if operation == "dept.update":
                return await tx.update_held(
                    _key(ResourceKind.DEPT, tid, target_id),
                    payload.model_dump(exclude_unset=True),
                )
            parent_id = payload.parent_id if new_id else payload.new_parent_id
            parent = next(
                (r for r in facts[ResourceKind.DEPT] if r["dept_id"] == parent_id), None
            )
            prefix = f"{parent['ancestors']},{parent_id}" if parent else "0"
            if new_id:
                await tx.insert_reserved(
                    _key(ResourceKind.DEPT, tid, new_id),
                    payload.model_dump()
                    | {
                        "parent_id": parent_id or None,
                        "ancestors": prefix,
                        "email": payload.email or None,
                    },
                )
            else:
                target = next(
                    r for r in facts[ResourceKind.DEPT] if r["dept_id"] == target_id
                )
                old = f"{target['ancestors']},{target_id}"
                for row in facts[ResourceKind.DEPT]:
                    ancestors = row["ancestors"] or ""
                    if ancestors == old or ancestors.startswith(old + ","):
                        await tx.update_held(
                            _key(ResourceKind.DEPT, tid, row["dept_id"]),
                            {
                                "ancestors": f"{prefix},{target_id}"
                                + ancestors[len(old) :]
                            },
                        )
                await tx.update_held(
                    _key(ResourceKind.DEPT, tid, target_id),
                    {"parent_id": parent_id, "ancestors": prefix},
                )
            return True
        return await self._delete(
            db, tx, tenant, operation, payload, target_id, facts, authority
        )

    async def _delete(
        self, db, tx, tenant, operation, payload, target_id, facts, authority
    ):
        if not operation.startswith("user.") and not authority.super_admin:
            raise AuthorizationException(
                "仅超级管理员可以删除", error_code="SUPER_ADMIN_REQUIRED"
            )
        ids = _ids(payload if operation.endswith("batch_delete") else [target_id])
        if not ids:
            raise _order_error()
        tid = tenant.tenant_id
        kind = {
            "user": ResourceKind.USER,
            "role": ResourceKind.ROLE,
            "dept": ResourceKind.DEPT,
        }[operation.split(".")[0]]
        id_name = resource_spec(kind).key_columns[0]
        rows = [row for row in facts[kind] if row[id_name] in ids]
        if len(rows) != len(ids):
            raise NotFoundException("目标资源")
        if kind == ResourceKind.ROLE:
            await assert_role_history_deletable(db, tx, tenant=tenant, role_ids=ids)
            if any(r["role_code"] == SUPER_ADMIN_ROLE_CODE for r in rows):
                raise BusinessRuleException(
                    "不能删除系统管理员角色", error_code="ROLE_DELETE_PROTECTED"
                )
            if any(r["role_id"] in ids for r in facts[ResourceKind.USER_ROLES]):
                raise BusinessRuleException(
                    "角色仍有关联成员", error_code="ROLE_DELETE_REFERENCED"
                )
            child_kinds = (
                ResourceKind.ROLE_AGENT,
                ResourceKind.ROLE_MENUS,
                ResourceKind.ROLE_DEPTS,
            )
        elif kind == ResourceKind.DEPT:
            if any(r["parent_id"] in ids for r in facts[kind]) or any(
                r["dept_id"] in ids
                for k in (ResourceKind.USER_DEPTS, ResourceKind.ROLE_DEPTS)
                for r in facts[k]
            ):
                raise BusinessRuleException(
                    "部门仍被引用", error_code="DEPT_DELETE_REFERENCED"
                )
            child_kinds = ()
        else:
            for user_id in ids:
                roles._ensure_target_visible(authority, user_id)
            if tenant.actor_user_id in ids or any(
                r["user_name"] == ADMIN_USERNAME for r in rows
            ):
                raise BusinessRuleException(
                    "不能删除受保护账户", error_code="USER_DELETE_PROTECTED"
                )
            # Existing context/FILE dependency owners are migrated separately.
            # Core delete refuses any remaining CASCADE/SET NULL dependency.
            child_kinds = (ResourceKind.USER_ROLES, ResourceKind.USER_DEPTS)
        for child_kind in child_kinds:
            spec = resource_spec(child_kind)
            for row in facts[child_kind]:
                if row[id_name] in ids:
                    await tx.delete_held(
                        _key(child_kind, tid, *(row[name] for name in spec.key_columns))
                    )
        for value in ids:
            await tx.delete_held(_key(kind, tid, value))
        return len(ids)


system_authorization_candidate = SystemAuthorizationCandidate()
