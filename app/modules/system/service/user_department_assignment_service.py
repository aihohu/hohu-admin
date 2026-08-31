"""Shared authorization policy for complete user-department replacements."""

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    ADMIN_USERNAME,
    IS_PRIMARY_NO,
    IS_PRIMARY_YES,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.id_generator import next_id
from app.db.base import user_depts
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import authorization_lock_service
from app.modules.system.service.authorization_snapshot import (
    materialized_role_set_snapshot,
)
from app.modules.system.service.config_service import config_service
from app.modules.system.service.grant_authority import (
    GrantAuthority,
    grant_authority_service,
)
from app.modules.system.service.user_role_assignment_service import (
    RoleSetAuthority,
    user_role_assignment_service,
)
from app.utils.data_scope import resolve_data_scope_for_roles

USER_EDIT_PERMISSION = "system:user:edit"
USER_ADD_PERMISSION = "system:user:add"
USER_IMPORT_PERMISSION = "system:user:import"
USER_LIST_PERMISSION = "system:user:list"
DEPT_LIST_PERMISSION = "system:dept:list"
DEPT_EDIT_PERMISSION = "system:dept:edit"


@dataclass(frozen=True)
class UserDepartmentAssignmentResult:
    """Stable before/after assignments returned by a complete replacement."""

    user_id: int
    old_assignments: tuple[tuple[int, bool], ...]
    new_assignments: tuple[tuple[int, bool], ...]


@dataclass(frozen=True)
class UserDepartmentAssignmentPreview:
    """Server-owned preview and snapshot for one complete replacement."""

    user_id: int
    user_name: str
    old_assignments: tuple[tuple[int, bool], ...]
    new_assignments: tuple[tuple[int, bool], ...]
    old_display: tuple[str, ...]
    new_display: tuple[str, ...]
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class DepartmentMemberRecord:
    """Minimal membership candidate safe for department administration."""

    user_id: int
    user_name: str
    nickname: str | None
    status: str
    is_member: bool
    is_primary: bool


@dataclass(frozen=True)
class DepartmentMemberPage:
    """Stable user-scoped page of department membership candidates."""

    current: int
    size: int
    total: int
    records: tuple[DepartmentMemberRecord, ...]


@dataclass(frozen=True)
class DepartmentMembershipResult:
    """Counts produced by one complete department member replacement."""

    added: int
    removed: int


def _role_ids(user: User) -> tuple[int, ...]:
    return tuple(sorted(int(role.role_id) for role in (user.roles or [])))


def _user_dept_ids(user: User) -> tuple[int, ...]:
    return tuple(sorted(int(dept.dept_id) for dept in (user.depts or [])))


def _role_custom_dept_ids(*users: User) -> set[int]:
    return {
        int(dept.dept_id)
        for user in users
        for role in (user.roles or [])
        for dept in (role.depts or [])
    }


class UserDepartmentAssignmentService:
    """Apply one department-assignment boundary to page and AI writers."""

    @staticmethod
    def _require_permissions(
        authority: GrantAuthority,
        required: frozenset[str],
    ) -> None:
        if authority.allows_permission_codes(required):
            return
        raise AuthorizationException(
            "权限不足",
            error_code="MISSING_PERMISSION",
        )

    async def ensure_create_permissions(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        has_departments: bool,
    ) -> GrantAuthority:
        """Fail before user creation when department assignment is unauthorized."""
        authority = await grant_authority_service.build(db, actor_user_id)
        required = {USER_ADD_PERMISSION}
        if has_departments:
            required.add(DEPT_LIST_PERMISSION)
        self._require_permissions(authority, frozenset(required))
        return authority

    async def ensure_import_permissions(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
    ) -> GrantAuthority:
        """Require import and department-list permissions for department input."""
        authority = await grant_authority_service.build(db, actor_user_id)
        self._require_permissions(
            authority,
            frozenset({USER_IMPORT_PERMISSION, DEPT_LIST_PERMISSION}),
        )
        return authority

    @staticmethod
    def _normalize_assignments(
        dept_assignments: list[tuple[int | str, bool]],
    ) -> tuple[tuple[int, bool], ...]:
        normalized: list[tuple[int, bool]] = []
        for dept_id, is_primary in dept_assignments:
            if (
                isinstance(dept_id, bool)
                or not isinstance(dept_id, (int, str))
                or (
                    isinstance(dept_id, str)
                    and re.fullmatch(r"[1-9][0-9]*", dept_id) is None
                )
                or not isinstance(is_primary, bool)
            ):
                raise BusinessRuleException(
                    "部门集合无效",
                    error_code="USER_DEPT_NOT_AVAILABLE",
                )
            try:
                normalized_id = int(dept_id)
            except (TypeError, ValueError) as exc:
                raise BusinessRuleException(
                    "部门集合无效",
                    error_code="USER_DEPT_NOT_AVAILABLE",
                ) from exc
            if normalized_id <= 0:
                raise BusinessRuleException(
                    "部门集合无效",
                    error_code="USER_DEPT_NOT_AVAILABLE",
                )
            normalized.append((normalized_id, is_primary))
        dept_ids = [dept_id for dept_id, _is_primary in normalized]
        if len(set(dept_ids)) != len(dept_ids):
            raise BusinessRuleException(
                "部门集合不能包含重复项",
                error_code="USER_DEPT_SET_DUPLICATE",
            )
        return tuple(sorted(normalized))

    @staticmethod
    def _assignment_payload(
        assignments: tuple[tuple[int, bool], ...],
    ) -> list[dict[str, Any]]:
        return [
            {"deptId": str(dept_id), "isPrimary": is_primary}
            for dept_id, is_primary in assignments
        ]

    @staticmethod
    def _assignment_display(
        assignments: tuple[tuple[int, bool], ...],
        dept_map: dict[int, Dept],
    ) -> tuple[str, ...]:
        return tuple(
            f"{'★ ' if is_primary else ''}{dept_map[dept_id].dept_name}"
            for dept_id, is_primary in assignments
        )

    @classmethod
    def _build_replacement_snapshot(
        cls,
        *,
        authority: GrantAuthority,
        target: User,
        old_assignments: tuple[tuple[int, bool], ...],
        new_assignments: tuple[tuple[int, bool], ...],
        old_authority: RoleSetAuthority,
        new_authority: RoleSetAuthority,
        require_primary: bool,
        dept_map: dict[int, Dept],
    ) -> dict[str, Any]:
        dept_facts = [
            {
                "deptId": str(dept.dept_id),
                "deptName": dept.dept_name,
                "parentId": str(dept.parent_id) if dept.parent_id else None,
                "ancestors": dept.ancestors,
                "status": dept.status,
            }
            for dept in sorted(dept_map.values(), key=lambda item: item.dept_id)
        ]
        return {
            "actor": {
                "userId": str(authority.actor_user_id),
                "status": authority.actor_status,
                "tenantId": str(authority.tenant_id),
                "superAdmin": authority.super_admin,
                "authorityVersion": authority.version_summary,
            },
            "target": {
                "userId": str(target.user_id),
                "userName": target.user_name,
                "status": target.status,
                "roleIds": [str(role_id) for role_id in _role_ids(target)],
            },
            "oldAssignments": cls._assignment_payload(old_assignments),
            "newAssignments": cls._assignment_payload(new_assignments),
            "departmentFacts": dept_facts,
            "requirePrimaryDepartment": require_primary,
            "before": materialized_role_set_snapshot(old_authority),
            "after": materialized_role_set_snapshot(new_authority),
        }

    async def _load_user(self, db: AsyncSession, user_id: int) -> User:
        user = await db.scalar(
            select(User)
            .where(User.user_id == user_id)
            .options(
                selectinload(User.roles).selectinload(Role.menus),
                selectinload(User.roles).selectinload(Role.depts),
                selectinload(User.depts),
            )
            .execution_options(populate_existing=True)
        )
        if user is None:
            raise NotFoundException("用户")
        return user

    async def _load_users(
        self,
        db: AsyncSession,
        user_ids: set[int],
    ) -> dict[int, User]:
        if not user_ids:
            return {}
        users = list(
            (
                await db.execute(
                    select(User)
                    .where(User.user_id.in_(user_ids))
                    .options(
                        selectinload(User.roles).selectinload(Role.menus),
                        selectinload(User.roles).selectinload(Role.depts),
                        selectinload(User.depts),
                    )
                    .order_by(User.user_id)
                    .execution_options(populate_existing=True)
                )
            )
            .unique()
            .scalars()
        )
        return {int(user.user_id): user for user in users}

    async def _load_roles(
        self,
        db: AsyncSession,
        role_ids: list[int],
    ) -> list[Role]:
        normalized = sorted({int(role_id) for role_id in role_ids})
        if len(normalized) != len(role_ids) or any(
            role_id <= 0 for role_id in normalized
        ):
            raise BusinessRuleException(
                "角色集合无效",
                error_code="USER_ROLE_NOT_AVAILABLE",
            )
        roles = list(
            (
                await db.execute(
                    select(Role)
                    .where(Role.role_id.in_(normalized))
                    .options(selectinload(Role.menus), selectinload(Role.depts))
                    .order_by(Role.role_id)
                    .execution_options(populate_existing=True)
                )
            )
            .unique()
            .scalars()
        )
        if len(roles) != len(normalized) or any(
            role.status != STATUS_ENABLED for role in roles
        ):
            raise BusinessRuleException(
                "角色不存在或已禁用",
                error_code="USER_ROLE_NOT_AVAILABLE",
            )
        return roles

    async def _load_assignments(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> tuple[tuple[int, bool], ...]:
        rows = (
            await db.execute(
                select(user_depts.c.dept_id, user_depts.c.is_primary)
                .where(user_depts.c.user_id == user_id)
                .order_by(user_depts.c.dept_id)
            )
        ).all()
        return tuple(
            (int(dept_id), str(is_primary) == IS_PRIMARY_YES)
            for dept_id, is_primary in rows
        )

    async def _load_assignments_by_user(
        self,
        db: AsyncSession,
        user_ids: set[int],
    ) -> dict[int, tuple[tuple[int, bool], ...]]:
        result: dict[int, list[tuple[int, bool]]] = {
            user_id: [] for user_id in user_ids
        }
        if not user_ids:
            return {}
        rows = (
            await db.execute(
                select(
                    user_depts.c.user_id,
                    user_depts.c.dept_id,
                    user_depts.c.is_primary,
                )
                .where(user_depts.c.user_id.in_(user_ids))
                .order_by(user_depts.c.user_id, user_depts.c.dept_id)
            )
        ).all()
        for user_id, dept_id, is_primary in rows:
            result[int(user_id)].append(
                (int(dept_id), str(is_primary) == IS_PRIMARY_YES)
            )
        return {user_id: tuple(items) for user_id, items in result.items()}

    async def _load_requested_dept_map(
        self,
        db: AsyncSession,
        assignment_sets: list[tuple[tuple[int, bool], ...]],
    ) -> dict[int, Dept]:
        dept_ids = {
            dept_id for assignments in assignment_sets for dept_id, _ in assignments
        }
        if not dept_ids:
            return {}
        depts = list(
            (
                await db.execute(
                    select(Dept)
                    .where(Dept.dept_id.in_(dept_ids))
                    .order_by(Dept.dept_id)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if len(depts) != len(dept_ids) or any(
            dept.status != STATUS_ENABLED for dept in depts
        ):
            raise BusinessRuleException(
                "部门不存在或已禁用",
                error_code="USER_DEPT_NOT_AVAILABLE",
            )
        return {int(dept.dept_id): dept for dept in depts}

    @staticmethod
    def _depts_for_assignments(
        assignments: tuple[tuple[int, bool], ...],
        dept_map: dict[int, Dept],
    ) -> list[Dept]:
        return [dept_map[dept_id] for dept_id, _is_primary in assignments]

    async def _load_requested_depts(
        self,
        db: AsyncSession,
        assignments: tuple[tuple[int, bool], ...],
    ) -> list[Dept]:
        dept_ids = [dept_id for dept_id, _is_primary in assignments]
        if not dept_ids:
            return []
        depts = list(
            (
                await db.execute(
                    select(Dept)
                    .where(Dept.dept_id.in_(dept_ids))
                    .order_by(Dept.dept_id)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if len(depts) != len(dept_ids) or any(
            dept.status != STATUS_ENABLED for dept in depts
        ):
            raise BusinessRuleException(
                "部门不存在或已禁用",
                error_code="USER_DEPT_NOT_AVAILABLE",
            )
        return depts

    async def _write_assignments(
        self,
        db: AsyncSession,
        *,
        target_user_id: int,
        assignments: tuple[tuple[int, bool], ...],
    ) -> None:
        await db.execute(
            delete(user_depts).where(user_depts.c.user_id == target_user_id)
        )
        if assignments:
            await db.execute(
                insert(user_depts),
                [
                    {
                        "user_id": target_user_id,
                        "dept_id": dept_id,
                        "is_primary": (IS_PRIMARY_YES if is_primary else IS_PRIMARY_NO),
                    }
                    for dept_id, is_primary in assignments
                ],
            )

    @staticmethod
    async def _materialized_dept_dependencies(
        db: AsyncSession,
        *,
        user: User,
        depts: list[Dept],
    ) -> set[int]:
        resolution = await resolve_data_scope_for_roles(
            db,
            user=user,
            roles=list(user.roles or []),
            depts=depts,
        )
        return (
            set()
            if resolution.accessible_dept_ids is None
            else set(resolution.accessible_dept_ids)
        )

    async def _lock_dependencies(
        self,
        db: AsyncSession,
        *,
        actor: User,
        target: User,
        requested_depts: list[Dept],
        actor_authority: GrantAuthority,
    ):
        old_scope_depts = await self._materialized_dept_dependencies(
            db,
            user=target,
            depts=list(target.depts or []),
        )
        new_scope_depts = await self._materialized_dept_dependencies(
            db,
            user=target,
            depts=requested_depts,
        )
        actor_scope_depts = (
            set()
            if actor_authority.accessible_dept_ids is None
            else set(actor_authority.accessible_dept_ids)
        )
        dept_ids = {
            *_user_dept_ids(actor),
            *_user_dept_ids(target),
            *(int(dept.dept_id) for dept in requested_depts),
            *_role_custom_dept_ids(actor, target),
            *actor_scope_depts,
            *old_scope_depts,
            *new_scope_depts,
        }
        return await authorization_lock_service.lock_targets(
            db,
            role_ids={*_role_ids(actor), *_role_ids(target)},
            dept_ids=dept_ids,
            user_ids={int(actor.user_id), int(target.user_id)},
        )

    @staticmethod
    def _ensure_primary_policy(
        assignments: tuple[tuple[int, bool], ...],
        *,
        require_primary: bool,
    ) -> None:
        primary_count = sum(is_primary for _dept_id, is_primary in assignments)
        if primary_count > 1:
            raise BusinessRuleException(
                "只能指定一个主部门",
                error_code="USER_PRIMARY_DEPT_MULTIPLE",
            )
        if require_primary and (not assignments or primary_count != 1):
            raise BusinessRuleException(
                "必须指定一个主部门",
                error_code="USER_PRIMARY_DEPT_REQUIRED",
            )

    @staticmethod
    def _ensure_target_protection(
        authority: GrantAuthority,
        target: User,
        *,
        prospective_roles: list[Role] | None = None,
    ) -> None:
        protected = target.user_name == ADMIN_USERNAME or any(
            role.status == STATUS_ENABLED and role.role_code == SUPER_ADMIN_ROLE_CODE
            for role in [*(target.roles or []), *(prospective_roles or [])]
        )
        if protected and not authority.super_admin:
            raise AuthorizationException(
                "只有超级管理员可以修改超级管理员账号的部门",
                error_code="AI_SUPER_ADMIN_REQUIRED",
            )

    @staticmethod
    def _ensure_direct_scope(
        authority: GrantAuthority,
        *,
        target_user_id: int,
        old_assignments: tuple[tuple[int, bool], ...],
        new_assignments: tuple[tuple[int, bool], ...],
        created_target: bool = False,
    ) -> None:
        if not created_target and not (
            authority.super_admin
            or authority.accessible_user_scope is None
            or target_user_id in authority.accessible_user_scope
        ):
            raise AuthorizationException(
                "目标用户不在当前数据权限范围内",
                error_code="AI_DATA_SCOPE_VIOLATION",
            )
        all_dept_ids = {
            dept_id for dept_id, _is_primary in (*old_assignments, *new_assignments)
        }
        if (
            authority.super_admin
            or authority.accessible_dept_ids is None
            or all_dept_ids <= authority.accessible_dept_ids
        ):
            return
        raise AuthorizationException(
            "部门不在当前数据权限范围内",
            error_code="AI_DATA_SCOPE_VIOLATION",
        )

    @staticmethod
    def _ensure_impact_dominated(
        authority: GrantAuthority,
        candidate: RoleSetAuthority,
        *,
        ignored_user_ids: frozenset[int] = frozenset(),
    ) -> None:
        try:
            user_role_assignment_service.ensure_role_set_dominated(
                authority,
                candidate,
                ignored_user_ids=ignored_user_ids,
            )
        except AuthorizationException as exc:
            raise AuthorizationException(
                "部门调整后的用户授权超出当前操作者的授权上界",
                error_code="AI_USER_DEPT_AUTHZ_IMPACT_OUT_OF_SCOPE",
            ) from exc

    @staticmethod
    def _live_dependency_ids(
        *,
        actor: User,
        target: User,
        requested_depts: list[Dept],
        authority: GrantAuthority,
        old_authority: RoleSetAuthority,
        new_authority: RoleSetAuthority,
    ) -> set[int]:
        return {
            *_user_dept_ids(actor),
            *_user_dept_ids(target),
            *(int(dept.dept_id) for dept in requested_depts),
            *_role_custom_dept_ids(actor, target),
            *(
                set()
                if authority.accessible_dept_ids is None
                else set(authority.accessible_dept_ids)
            ),
            *(
                set()
                if old_authority.accessible_dept_ids is None
                else set(old_authority.accessible_dept_ids)
            ),
            *(
                set()
                if new_authority.accessible_dept_ids is None
                else set(new_authority.accessible_dept_ids)
            ),
        }

    async def _authorize_assignment_change(
        self,
        db: AsyncSession,
        *,
        authority: GrantAuthority,
        target: User,
        old_assignments: tuple[tuple[int, bool], ...],
        new_assignments: tuple[tuple[int, bool], ...],
        requested_depts: list[Dept],
        prospective_roles: list[Role] | None = None,
        created_target: bool = False,
        ignored_user_ids: frozenset[int] = frozenset(),
        require_primary: bool | None = None,
        old_authority: RoleSetAuthority | None = None,
        new_authority: RoleSetAuthority | None = None,
    ) -> tuple[RoleSetAuthority, RoleSetAuthority]:
        """Validate one complete old/new assignment set without writing it."""
        self._ensure_direct_scope(
            authority,
            target_user_id=int(target.user_id),
            old_assignments=old_assignments,
            new_assignments=new_assignments,
            created_target=created_target,
        )
        final_roles = (
            list(target.roles or []) if prospective_roles is None else prospective_roles
        )
        self._ensure_target_protection(
            authority,
            target,
            prospective_roles=final_roles,
        )
        if require_primary is None:
            require_primary = await config_service.get_bool_for_update(
                db,
                "user_require_primary_dept",
            )
        self._ensure_primary_policy(new_assignments, require_primary=require_primary)
        if old_authority is None:
            old_authority = (
                await user_role_assignment_service.materialize_role_set_authority(
                    db,
                    user=target,
                    roles=list(target.roles or []),
                    depts=list(target.depts or []),
                )
            )
        if new_authority is None:
            new_authority = (
                await user_role_assignment_service.materialize_role_set_authority(
                    db,
                    user=target,
                    roles=final_roles,
                    depts=requested_depts,
                )
            )
        effective_ignored_ids = ignored_user_ids
        if created_target:
            effective_ignored_ids = effective_ignored_ids | frozenset(
                {int(target.user_id)}
            )
        if not created_target:
            self._ensure_impact_dominated(
                authority,
                old_authority,
                ignored_user_ids=effective_ignored_ids,
            )
        self._ensure_impact_dominated(
            authority,
            new_authority,
            ignored_user_ids=effective_ignored_ids,
        )
        return old_authority, new_authority

    async def preview_departments(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        dept_assignments: list[tuple[int | str, bool]],
    ) -> UserDepartmentAssignmentPreview:
        """Validate and freeze one complete replacement without writing it."""
        normalized = self._normalize_assignments(dept_assignments)
        authority = await grant_authority_service.build(db, actor_user_id)
        self._require_permissions(
            authority,
            frozenset({USER_EDIT_PERMISSION, DEPT_LIST_PERMISSION}),
        )
        target = await self._load_user(db, target_user_id)
        old_assignments = await self._load_assignments(db, target_user_id)
        requested_depts = await self._load_requested_depts(db, normalized)
        require_primary = await config_service.get_bool_for_update(
            db,
            "user_require_primary_dept",
        )
        old_authority, new_authority = await self._authorize_assignment_change(
            db,
            authority=authority,
            target=target,
            old_assignments=old_assignments,
            new_assignments=normalized,
            requested_depts=requested_depts,
            require_primary=require_primary,
        )
        dept_map = {
            int(dept.dept_id): dept
            for dept in [*(target.depts or []), *requested_depts]
        }
        return UserDepartmentAssignmentPreview(
            user_id=int(target.user_id),
            user_name=target.user_name,
            old_assignments=old_assignments,
            new_assignments=normalized,
            old_display=self._assignment_display(old_assignments, dept_map),
            new_display=self._assignment_display(normalized, dept_map),
            snapshot=self._build_replacement_snapshot(
                authority=authority,
                target=target,
                old_assignments=old_assignments,
                new_assignments=normalized,
                old_authority=old_authority,
                new_authority=new_authority,
                require_primary=require_primary,
                dept_map=dept_map,
            ),
        )

    async def _replace_departments(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        dept_assignments: list[tuple[int | str, bool]],
        required_permissions: frozenset[str],
        created_target: bool,
        ignored_user_ids: frozenset[int] = frozenset(),
        expected_snapshot: dict[str, Any] | None = None,
    ) -> UserDepartmentAssignmentResult:
        normalized = self._normalize_assignments(dept_assignments)
        actor_before = await self._load_user(db, actor_user_id)
        authority_before = await grant_authority_service.build(db, actor_user_id)
        self._require_permissions(authority_before, required_permissions)
        target_before = await self._load_user(db, target_user_id)
        actor_role_snapshot = _role_ids(actor_before)
        actor_dept_snapshot = _user_dept_ids(actor_before)
        target_role_snapshot = _role_ids(target_before)
        target_assignment_snapshot = await self._load_assignments(
            db,
            target_user_id,
        )
        if created_target and target_assignment_snapshot:
            raise BusinessRuleException(
                "新用户部门快照已发生变化，请重试",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        self._ensure_direct_scope(
            authority_before,
            target_user_id=target_user_id,
            old_assignments=target_assignment_snapshot,
            new_assignments=normalized,
            created_target=created_target,
        )
        self._ensure_target_protection(authority_before, target_before)
        requested_before = await self._load_requested_depts(db, normalized)
        locked = await self._lock_dependencies(
            db,
            actor=actor_before,
            target=target_before,
            requested_depts=requested_before,
            actor_authority=authority_before,
        )

        actor = await self._load_user(db, actor_user_id)
        target = await self._load_user(db, target_user_id)
        current_assignments = await self._load_assignments(db, target_user_id)
        if (
            _role_ids(actor) != actor_role_snapshot
            or _user_dept_ids(actor) != actor_dept_snapshot
            or _role_ids(target) != target_role_snapshot
            or current_assignments != target_assignment_snapshot
        ):
            raise BusinessRuleException(
                "授权事实已变化，请重试",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )

        authority = await grant_authority_service.build(db, actor_user_id)
        self._require_permissions(authority, required_permissions)
        requested_depts = await self._load_requested_depts(db, normalized)
        require_primary = await config_service.get_bool_for_update(
            db,
            "user_require_primary_dept",
        )
        old_authority, new_authority = await self._authorize_assignment_change(
            db,
            authority=authority,
            target=target,
            old_assignments=current_assignments,
            new_assignments=normalized,
            requested_depts=requested_depts,
            created_target=created_target,
            ignored_user_ids=ignored_user_ids,
            require_primary=require_primary,
        )
        live_dependency_ids = self._live_dependency_ids(
            actor=actor,
            target=target,
            requested_depts=requested_depts,
            authority=authority,
            old_authority=old_authority,
            new_authority=new_authority,
        )
        if not live_dependency_ids <= set(locked.dept_ids):
            raise BusinessRuleException(
                "授权事实已变化，请重试",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )

        if expected_snapshot is not None:
            dept_map = {
                int(dept.dept_id): dept
                for dept in [*(target.depts or []), *requested_depts]
            }
            current_snapshot = self._build_replacement_snapshot(
                authority=authority,
                target=target,
                old_assignments=current_assignments,
                new_assignments=normalized,
                old_authority=old_authority,
                new_authority=new_authority,
                require_primary=require_primary,
                dept_map=dept_map,
            )
            if current_snapshot != expected_snapshot:
                raise BusinessRuleException(
                    "审批快照已变化，请重新确认",
                    error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
                )

        await self._write_assignments(
            db,
            target_user_id=target_user_id,
            assignments=normalized,
        )
        return UserDepartmentAssignmentResult(
            user_id=target_user_id,
            old_assignments=current_assignments,
            new_assignments=normalized,
        )

    async def replace_departments(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        dept_assignments: list[tuple[int | str, bool]],
        expected_snapshot: dict[str, Any] | None = None,
    ) -> UserDepartmentAssignmentResult:
        """Replace one existing user's complete department set without committing."""
        return await self._replace_departments(
            db,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            dept_assignments=dept_assignments,
            required_permissions=frozenset(
                {USER_EDIT_PERMISSION, DEPT_LIST_PERMISSION}
            ),
            created_target=False,
            expected_snapshot=expected_snapshot,
        )

    async def assign_created_user_departments(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        dept_assignments: list[tuple[int | str, bool]],
    ) -> UserDepartmentAssignmentResult:
        """Assign a newly created user's complete department set."""
        required = {USER_ADD_PERMISSION}
        if dept_assignments:
            required.add(DEPT_LIST_PERMISSION)
        return await self._replace_departments(
            db,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            dept_assignments=dept_assignments,
            required_permissions=frozenset(required),
            created_target=True,
        )

    async def replace_imported_user_departments(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        dept_assignments: list[tuple[int | str, bool]],
        created_target: bool,
        ignored_user_ids: frozenset[int] = frozenset(),
    ) -> UserDepartmentAssignmentResult:
        """Apply an imported user's complete department set under import policy."""
        return await self._replace_departments(
            db,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            dept_assignments=dept_assignments,
            required_permissions=frozenset(
                {USER_IMPORT_PERMISSION, DEPT_LIST_PERMISSION}
            ),
            created_target=created_target,
            ignored_user_ids=ignored_user_ids,
        )

    async def validate_import_department_assignment(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int | None,
        target_user_name: str,
        target_status: str,
        role_ids: list[int],
        dept_assignments: list[tuple[int | str, bool]],
        authority: GrantAuthority | None = None,
        prospective_user_id: int | None = None,
        ignored_user_ids: frozenset[int] = frozenset(),
    ) -> None:
        """Validate one frozen import target without writing associations."""
        normalized = self._normalize_assignments(dept_assignments)
        requested_depts = await self._load_requested_depts(db, normalized)
        prospective_roles = await self._load_roles(db, role_ids)
        created_target = target_user_id is None
        if created_target:
            target = User(
                user_id=prospective_user_id or next_id(),
                user_name=target_user_name,
                nickname=target_user_name,
                hashed_password="",
                status=target_status,
                roles=prospective_roles,
                depts=[],
            )
            old_assignments: tuple[tuple[int, bool], ...] = ()
        else:
            target = await self._load_user(db, target_user_id)
            old_assignments = await self._load_assignments(db, target_user_id)
        live_authority = authority or await grant_authority_service.build(
            db,
            actor_user_id,
        )
        await self._authorize_assignment_change(
            db,
            authority=live_authority,
            target=target,
            old_assignments=old_assignments,
            new_assignments=normalized,
            requested_depts=requested_depts,
            prospective_roles=prospective_roles,
            created_target=created_target,
            ignored_user_ids=ignored_user_ids,
        )

    async def apply_locked_import_departments(
        self,
        db: AsyncSession,
        *,
        target_user_id: int,
        dept_assignments: list[tuple[int | str, bool]],
    ) -> UserDepartmentAssignmentResult:
        """Apply a set already validated under the import batch authorization lock."""
        normalized = self._normalize_assignments(dept_assignments)
        old_assignments = await self._load_assignments(db, target_user_id)
        await self._write_assignments(
            db,
            target_user_id=target_user_id,
            assignments=normalized,
        )
        return UserDepartmentAssignmentResult(
            user_id=target_user_id,
            old_assignments=old_assignments,
            new_assignments=normalized,
        )

    @staticmethod
    def _ensure_department_scope(
        authority: GrantAuthority,
        dept_id: int,
    ) -> None:
        if (
            authority.super_admin
            or authority.accessible_dept_ids is None
            or dept_id in authority.accessible_dept_ids
        ):
            return
        raise AuthorizationException(
            "部门不在当前数据权限范围内",
            error_code="AI_DATA_SCOPE_VIOLATION",
        )

    async def _load_department(self, db: AsyncSession, dept_id: int) -> Dept:
        dept = await db.scalar(
            select(Dept)
            .where(Dept.dept_id == dept_id)
            .execution_options(populate_existing=True)
        )
        if dept is None:
            raise NotFoundException("部门")
        return dept

    async def _load_department_snapshot(
        self,
        db: AsyncSession,
        dept_ids: set[int],
    ) -> tuple[tuple[int, int | None, str | None, str], ...]:
        """Return stable structural facts for every department dependency."""
        if not dept_ids:
            return ()
        rows = (
            await db.execute(
                select(
                    Dept.dept_id,
                    Dept.parent_id,
                    Dept.ancestors,
                    Dept.status,
                )
                .where(Dept.dept_id.in_(dept_ids))
                .order_by(Dept.dept_id)
            )
        ).all()
        if len(rows) != len(dept_ids):
            raise BusinessRuleException(
                "授权事实已变化，请重试",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        return tuple(
            (
                int(dept_id),
                int(parent_id) if parent_id is not None else None,
                ancestors,
                str(status),
            )
            for dept_id, parent_id, ancestors, status in rows
        )

    async def _load_member_map(
        self,
        db: AsyncSession,
        dept_id: int,
    ) -> dict[int, bool]:
        rows = (
            await db.execute(
                select(user_depts.c.user_id, user_depts.c.is_primary).where(
                    user_depts.c.dept_id == dept_id
                )
            )
        ).all()
        return {
            int(user_id): str(is_primary) == IS_PRIMARY_YES
            for user_id, is_primary in rows
        }

    @staticmethod
    def _ensure_member_scope(
        authority: GrantAuthority,
        user_ids: set[int],
    ) -> None:
        if (
            authority.super_admin
            or authority.accessible_user_scope is None
            or user_ids <= authority.accessible_user_scope
        ):
            return
        raise AuthorizationException(
            "部门成员集合包含不可管理用户",
            error_code="DEPT_MEMBERSHIP_GLOBAL_IMPACT_OUT_OF_SCOPE",
        )

    async def list_department_members(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        dept_id: int,
        query: str | None,
        current: int,
        size: int,
    ) -> DepartmentMemberPage:
        """Return one minimal, user-scoped candidate page without leaking members."""
        authority = await grant_authority_service.build(db, actor_user_id)
        self._require_permissions(
            authority,
            frozenset(
                {DEPT_LIST_PERMISSION, DEPT_EDIT_PERMISSION, USER_LIST_PERMISSION}
            ),
        )
        await self._load_department(db, dept_id)
        self._ensure_department_scope(authority, dept_id)
        member_map = await self._load_member_map(db, dept_id)
        self._ensure_member_scope(authority, set(member_map))

        filters = [
            or_(User.status == STATUS_ENABLED, User.user_id.in_(set(member_map)))
        ]
        if authority.accessible_user_scope is not None:
            filters.append(User.user_id.in_(authority.accessible_user_scope))
        normalized_query = (query or "").strip()
        if normalized_query:
            pattern = f"%{normalized_query}%"
            filters.append(
                or_(User.user_name.ilike(pattern), User.nickname.ilike(pattern))
            )
        total = int(
            await db.scalar(select(func.count()).select_from(User).where(*filters)) or 0
        )
        users = list(
            (
                await db.execute(
                    select(User)
                    .where(*filters)
                    .order_by(User.user_id)
                    .offset((current - 1) * size)
                    .limit(size)
                )
            ).scalars()
        )
        return DepartmentMemberPage(
            current=current,
            size=size,
            total=total,
            records=tuple(
                DepartmentMemberRecord(
                    user_id=int(user.user_id),
                    user_name=user.user_name,
                    nickname=user.nickname,
                    status=user.status,
                    is_member=int(user.user_id) in member_map,
                    is_primary=member_map.get(int(user.user_id), False),
                )
                for user in users
            ),
        )

    async def replace_department_members(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        dept_id: int,
        user_ids: list[int | str],
    ) -> DepartmentMembershipResult:
        """Replace a department's complete member set atomically without committing."""
        normalized_user_ids: list[int] = []
        for user_id in user_ids:
            if (
                isinstance(user_id, bool)
                or not isinstance(user_id, (int, str))
                or (
                    isinstance(user_id, str)
                    and re.fullmatch(r"[1-9][0-9]*", user_id) is None
                )
            ):
                raise BusinessRuleException(
                    "用户集合无效",
                    error_code="USER_DEPT_MEMBER_NOT_AVAILABLE",
                )
            normalized_user_ids.append(int(user_id))
        if len(set(normalized_user_ids)) != len(normalized_user_ids) or any(
            user_id <= 0 for user_id in normalized_user_ids
        ):
            raise BusinessRuleException(
                "用户集合无效",
                error_code="USER_DEPT_MEMBER_NOT_AVAILABLE",
            )

        actor_before = await self._load_user(db, actor_user_id)
        authority_before = await grant_authority_service.build(db, actor_user_id)
        required_permissions = frozenset(
            {DEPT_LIST_PERMISSION, DEPT_EDIT_PERMISSION, USER_EDIT_PERMISSION}
        )
        self._require_permissions(authority_before, required_permissions)
        dept_before = await self._load_department(db, dept_id)
        self._ensure_department_scope(authority_before, dept_id)
        member_map_before = await self._load_member_map(db, dept_id)
        current_ids = set(member_map_before)
        requested_ids = set(normalized_user_ids)
        self._ensure_member_scope(
            authority_before,
            current_ids | requested_ids,
        )
        to_add = requested_ids - current_ids
        to_remove = current_ids - requested_ids
        if any(member_map_before[user_id] for user_id in to_remove):
            raise BusinessRuleException(
                "主部门成员必须通过用户部门接口重新指定主部门",
                error_code="USER_PRIMARY_DEPT_REASSIGN_REQUIRED",
            )

        affected_id_set = to_add | to_remove
        affected_ids = sorted(affected_id_set)
        affected_by_id = await self._load_users(db, affected_id_set)
        if set(affected_by_id) != affected_id_set:
            raise BusinessRuleException(
                "用户不存在或不可分配",
                error_code="USER_DEPT_MEMBER_NOT_AVAILABLE",
            )
        if any(affected_by_id[user_id].status != STATUS_ENABLED for user_id in to_add):
            raise BusinessRuleException(
                "禁用用户不能新增为部门成员",
                error_code="USER_DEPT_MEMBER_NOT_AVAILABLE",
            )
        if to_add and dept_before.status != STATUS_ENABLED:
            raise BusinessRuleException(
                "部门不存在或已禁用",
                error_code="USER_DEPT_NOT_AVAILABLE",
            )

        actor_role_snapshot = _role_ids(actor_before)
        actor_dept_snapshot = _user_dept_ids(actor_before)
        assignments_before = await self._load_assignments_by_user(
            db,
            affected_id_set,
        )
        target_snapshots = {
            user_id: (
                _role_ids(user),
                assignments_before[user_id],
                user.status,
            )
            for user_id, user in affected_by_id.items()
        }
        prospective_assignments: dict[int, tuple[tuple[int, bool], ...]] = {}
        for user_id in affected_ids:
            old_assignments = assignments_before[user_id]
            prospective_assignments[user_id] = (
                tuple(sorted((*old_assignments, (dept_id, False))))
                if user_id in to_add
                else tuple(
                    assignment
                    for assignment in old_assignments
                    if assignment[0] != dept_id
                )
            )
        requested_dept_map_before = await self._load_requested_dept_map(
            db,
            list(prospective_assignments.values()),
        )
        requested_depts_before = {
            user_id: self._depts_for_assignments(
                assignments,
                requested_dept_map_before,
            )
            for user_id, assignments in prospective_assignments.items()
        }
        authority_candidates_before = [
            candidate
            for user_id in affected_ids
            for candidate in (
                (
                    affected_by_id[user_id],
                    list(affected_by_id[user_id].roles or []),
                    list(affected_by_id[user_id].depts or []),
                ),
                (
                    affected_by_id[user_id],
                    list(affected_by_id[user_id].roles or []),
                    requested_depts_before[user_id],
                ),
            )
        ]
        materialized_before = (
            await user_role_assignment_service.materialize_role_set_authorities(
                db,
                candidates=authority_candidates_before,
            )
        )
        authorities_before = {
            user_id: (
                materialized_before[index * 2],
                materialized_before[index * 2 + 1],
            )
            for index, user_id in enumerate(affected_ids)
        }
        dependency_dept_ids = {
            dept_id,
            *_user_dept_ids(actor_before),
            *_role_custom_dept_ids(actor_before, *affected_by_id.values()),
            *(
                set()
                if authority_before.accessible_dept_ids is None
                else set(authority_before.accessible_dept_ids)
            ),
        }
        for user_id in affected_ids:
            old_authority, new_authority = authorities_before[user_id]
            dependency_dept_ids.update(
                dept_id
                for candidate in (old_authority, new_authority)
                if candidate.accessible_dept_ids is not None
                for dept_id in candidate.accessible_dept_ids
            )
            dependency_dept_ids.update(
                dept_id
                for assignments in (
                    assignments_before[user_id],
                    prospective_assignments[user_id],
                )
                for dept_id, _is_primary in assignments
            )

        dependency_dept_snapshot = await self._load_department_snapshot(
            db,
            dependency_dept_ids,
        )

        locked = await authorization_lock_service.lock_targets(
            db,
            role_ids={
                *_role_ids(actor_before),
                *(
                    role_id
                    for user in affected_by_id.values()
                    for role_id in _role_ids(user)
                ),
            },
            dept_ids=dependency_dept_ids,
            user_ids={actor_user_id, *current_ids, *requested_ids},
        )
        actor = await self._load_user(db, actor_user_id)
        authority = await grant_authority_service.build(db, actor_user_id)
        dept = await self._load_department(db, dept_id)
        member_map = await self._load_member_map(db, dept_id)
        if (
            _role_ids(actor) != actor_role_snapshot
            or _user_dept_ids(actor) != actor_dept_snapshot
            or authority.version_summary != authority_before.version_summary
            or dept.status != dept_before.status
            or member_map != member_map_before
            or await self._load_department_snapshot(db, dependency_dept_ids)
            != dependency_dept_snapshot
        ):
            raise BusinessRuleException(
                "授权事实已变化，请重试",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        self._require_permissions(authority, required_permissions)
        self._ensure_department_scope(authority, dept_id)
        self._ensure_member_scope(authority, current_ids | requested_ids)

        affected = await self._load_users(db, affected_id_set)
        current_assignments_by_user = await self._load_assignments_by_user(
            db,
            affected_id_set,
        )
        for user_id in affected_ids:
            target = affected[user_id]
            current_assignments = current_assignments_by_user[user_id]
            expected_roles, expected_assignments, expected_status = target_snapshots[
                user_id
            ]
            if (
                _role_ids(target) != expected_roles
                or current_assignments != expected_assignments
                or target.status != expected_status
            ):
                raise BusinessRuleException(
                    "授权事实已变化，请重试",
                    error_code="AUTHORIZATION_SNAPSHOT_STALE",
                )

        requested_dept_map = await self._load_requested_dept_map(
            db,
            list(prospective_assignments.values()),
        )
        requested_depts_by_user = {
            user_id: self._depts_for_assignments(
                assignments,
                requested_dept_map,
            )
            for user_id, assignments in prospective_assignments.items()
        }
        authority_candidates = [
            candidate
            for user_id in affected_ids
            for candidate in (
                (
                    affected[user_id],
                    list(affected[user_id].roles or []),
                    list(affected[user_id].depts or []),
                ),
                (
                    affected[user_id],
                    list(affected[user_id].roles or []),
                    requested_depts_by_user[user_id],
                ),
            )
        ]
        materialized = (
            await user_role_assignment_service.materialize_role_set_authorities(
                db,
                candidates=authority_candidates,
            )
        )
        if materialized != materialized_before:
            raise BusinessRuleException(
                "授权事实已变化，请重试",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        require_primary = await config_service.get_bool_for_update(
            db,
            "user_require_primary_dept",
        )
        for index, user_id in enumerate(affected_ids):
            target = affected[user_id]
            current_assignments = current_assignments_by_user[user_id]
            old_authority, new_authority = await self._authorize_assignment_change(
                db,
                authority=authority,
                target=target,
                old_assignments=current_assignments,
                new_assignments=prospective_assignments[user_id],
                requested_depts=requested_depts_by_user[user_id],
                require_primary=require_primary,
                old_authority=materialized[index * 2],
                new_authority=materialized[index * 2 + 1],
            )
            live_dependencies = self._live_dependency_ids(
                actor=actor,
                target=target,
                requested_depts=requested_depts_by_user[user_id],
                authority=authority,
                old_authority=old_authority,
                new_authority=new_authority,
            )
            if not live_dependencies <= set(locked.dept_ids):
                raise BusinessRuleException(
                    "授权事实已变化，请重试",
                    error_code="AUTHORIZATION_SNAPSHOT_STALE",
                )

        if to_remove:
            await db.execute(
                delete(user_depts).where(
                    user_depts.c.dept_id == dept_id,
                    user_depts.c.user_id.in_(to_remove),
                )
            )
        if to_add:
            await db.execute(
                insert(user_depts),
                [
                    {
                        "user_id": user_id,
                        "dept_id": dept_id,
                        "is_primary": IS_PRIMARY_NO,
                    }
                    for user_id in sorted(to_add)
                ],
            )
        return DepartmentMembershipResult(
            added=len(to_add),
            removed=len(to_remove),
        )


user_department_assignment_service = UserDepartmentAssignmentService()
