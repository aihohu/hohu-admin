"""Shared authorization policy for complete user-department replacements."""

import re
from dataclasses import dataclass

from sqlalchemy import delete, insert, select
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
from app.db.base import user_depts
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import authorization_lock_service
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
DEPT_LIST_PERMISSION = "system:dept:list"


@dataclass(frozen=True)
class UserDepartmentAssignmentResult:
    """Stable before/after assignments returned by a complete replacement."""

    user_id: int
    old_assignments: tuple[tuple[int, bool], ...]
    new_assignments: tuple[tuple[int, bool], ...]


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
    def _require_permissions(authority: GrantAuthority) -> None:
        required = frozenset({USER_EDIT_PERMISSION, DEPT_LIST_PERMISSION})
        if authority.allows_permission_codes(required):
            return
        raise AuthorizationException(
            "权限不足",
            error_code="MISSING_PERMISSION",
        )

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
    def _ensure_target_protection(authority: GrantAuthority, target: User) -> None:
        protected = target.user_name == ADMIN_USERNAME or any(
            role.status == STATUS_ENABLED and role.role_code == SUPER_ADMIN_ROLE_CODE
            for role in (target.roles or [])
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
    ) -> None:
        if not (
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
    ) -> None:
        try:
            user_role_assignment_service.ensure_role_set_dominated(
                authority,
                candidate,
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

    async def replace_departments(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        dept_assignments: list[tuple[int | str, bool]],
    ) -> UserDepartmentAssignmentResult:
        """Replace one existing user's complete department set without committing."""
        normalized = self._normalize_assignments(dept_assignments)
        actor_before = await self._load_user(db, actor_user_id)
        authority_before = await grant_authority_service.build(db, actor_user_id)
        self._require_permissions(authority_before)
        target_before = await self._load_user(db, target_user_id)
        actor_role_snapshot = _role_ids(actor_before)
        actor_dept_snapshot = _user_dept_ids(actor_before)
        target_role_snapshot = _role_ids(target_before)
        target_assignment_snapshot = await self._load_assignments(
            db,
            target_user_id,
        )
        self._ensure_direct_scope(
            authority_before,
            target_user_id=target_user_id,
            old_assignments=target_assignment_snapshot,
            new_assignments=normalized,
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
        self._require_permissions(authority)
        self._ensure_direct_scope(
            authority,
            target_user_id=target_user_id,
            old_assignments=current_assignments,
            new_assignments=normalized,
        )
        self._ensure_target_protection(authority, target)
        requested_depts = await self._load_requested_depts(db, normalized)
        old_authority = (
            await user_role_assignment_service.materialize_role_set_authority(
                db,
                user=target,
                roles=list(target.roles or []),
                depts=list(target.depts or []),
            )
        )
        new_authority = (
            await user_role_assignment_service.materialize_role_set_authority(
                db,
                user=target,
                roles=list(target.roles or []),
                depts=requested_depts,
            )
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

        require_primary = await config_service.get_bool_for_update(
            db,
            "user_require_primary_dept",
        )
        self._ensure_primary_policy(normalized, require_primary=require_primary)
        self._ensure_impact_dominated(authority, old_authority)
        self._ensure_impact_dominated(authority, new_authority)

        await db.execute(
            delete(user_depts).where(user_depts.c.user_id == target_user_id)
        )
        if normalized:
            await db.execute(
                insert(user_depts),
                [
                    {
                        "user_id": target_user_id,
                        "dept_id": dept_id,
                        "is_primary": (IS_PRIMARY_YES if is_primary else IS_PRIMARY_NO),
                    }
                    for dept_id, is_primary in normalized
                ],
            )
        return UserDepartmentAssignmentResult(
            user_id=target_user_id,
            old_assignments=current_assignments,
            new_assignments=normalized,
        )


user_department_assignment_service = UserDepartmentAssignmentService()
