"""Shared authorization policy for complete user-role replacements."""

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    ADMIN_USERNAME,
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
    USER_ROLE_CODE,
)
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.id_generator import next_id
from app.db.base import user_depts
from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import authorization_lock_service
from app.modules.system.service.grant_authority import (
    GrantAuthority,
    grant_authority_service,
)
from app.utils.data_scope import get_dept_and_sub_ids, resolve_data_scope_for_roles

USER_ADD_PERMISSION = "system:user:add"
USER_EDIT_PERMISSION = "system:user:edit"
USER_IMPORT_PERMISSION = "system:user:import"


@dataclass(frozen=True)
class RoleSetAuthority:
    """Materialized effective authorization contributed by one complete role set."""

    permission_codes: frozenset[str]
    menu_ids: frozenset[int]
    agent_ids: frozenset[int]
    accessible_dept_ids: frozenset[int] | None
    accessible_user_ids: frozenset[int] | None
    role_definition_signature: tuple[
        tuple[
            int,
            str,
            str,
            str,
            tuple[int, ...],
            tuple[int, ...],
            tuple[int, ...],
        ],
        ...,
    ]


@dataclass(frozen=True)
class UserRoleAssignmentResult:
    """Stable before/after identifiers returned by a role replacement."""

    user_id: int
    old_role_ids: tuple[int, ...]
    new_role_ids: tuple[int, ...]


def _role_ids(roles: list[Role]) -> tuple[int, ...]:
    return tuple(sorted(int(role.role_id) for role in roles))


def _dept_ids(user: User, roles: list[Role]) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                *(int(dept.dept_id) for dept in (user.depts or [])),
                *(int(dept.dept_id) for role in roles for dept in (role.depts or [])),
            }
        )
    )


class UserRoleAssignmentService:
    """Apply the same role-delegation boundary to page and AI writers."""

    @staticmethod
    def _require_permissions(
        authority: GrantAuthority,
        required_permissions: frozenset[str],
    ) -> None:
        if authority.allows_permission_codes(required_permissions):
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
        explicit_roles: bool,
    ) -> GrantAuthority:
        """Fail before user creation when the selected entry permissions are absent."""
        authority = await grant_authority_service.build(db, actor_user_id)
        required = {USER_ADD_PERMISSION}
        if explicit_roles:
            required.add(USER_ROLE_AUTH_PERMISSION)
        self._require_permissions(authority, frozenset(required))
        return authority

    async def ensure_import_permissions(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        has_role_column: bool,
    ) -> GrantAuthority:
        """Require role delegation before an import with an explicit role column."""
        authority = await grant_authority_service.build(db, actor_user_id)
        required = {USER_IMPORT_PERMISSION}
        if has_role_column:
            required.add(USER_ROLE_AUTH_PERMISSION)
        self._require_permissions(authority, frozenset(required))
        return authority

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

    async def _load_requested_roles(
        self,
        db: AsyncSession,
        role_ids: list[int | str],
    ) -> list[Role]:
        normalized = [int(role_id) for role_id in role_ids]
        if not normalized:
            raise BusinessRuleException(
                "用户必须至少分配一个角色",
                error_code="USER_ROLE_SET_REQUIRED",
            )
        if len(set(normalized)) != len(normalized):
            raise BusinessRuleException(
                "角色集合不能包含重复项",
                error_code="USER_ROLE_SET_DUPLICATE",
            )
        if any(role_id <= 0 for role_id in normalized):
            raise BusinessRuleException(
                "角色标识无效",
                error_code="USER_ROLE_NOT_AVAILABLE",
            )
        roles = (
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
            .all()
        )
        if len(roles) != len(normalized) or any(
            role.status != STATUS_ENABLED for role in roles
        ):
            raise BusinessRuleException(
                "角色不存在或已禁用",
                error_code="USER_ROLE_NOT_AVAILABLE",
            )
        return list(roles)

    async def _default_role(self, db: AsyncSession) -> Role:
        role = await db.scalar(
            select(Role)
            .where(
                Role.role_code == USER_ROLE_CODE,
                Role.status == STATUS_ENABLED,
            )
            .options(selectinload(Role.menus), selectinload(Role.depts))
            .execution_options(populate_existing=True)
        )
        if role is None:
            raise BusinessRuleException(
                "默认角色不存在或已禁用",
                error_code="USER_DEFAULT_ROLE_NOT_AVAILABLE",
            )
        return role

    async def _load_prospective_depts(
        self,
        db: AsyncSession,
        dept_ids: list[int],
    ) -> list[Dept]:
        normalized = [int(dept_id) for dept_id in dept_ids]
        if len(set(normalized)) != len(normalized) or any(
            dept_id <= 0 for dept_id in normalized
        ):
            raise BusinessRuleException(
                "部门集合无效",
                error_code="USER_DEPT_NOT_AVAILABLE",
            )
        if not normalized:
            return []
        depts = list(
            (
                await db.execute(
                    select(Dept)
                    .where(Dept.dept_id.in_(normalized))
                    .order_by(Dept.dept_id)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if len(depts) != len(normalized) or any(
            dept.status != STATUS_ENABLED for dept in depts
        ):
            raise BusinessRuleException(
                "部门不存在或已禁用",
                error_code="USER_DEPT_NOT_AVAILABLE",
            )
        return depts

    async def materialize_role_set_authority(
        self,
        db: AsyncSession,
        *,
        user: User,
        roles: list[Role],
        depts: list[Dept] | None = None,
    ) -> RoleSetAuthority:
        from app.modules.ai.service.agent_authorization_service import (  # noqa: PLC0415
            agent_authorization_service,
        )

        enabled_roles = [role for role in roles if role.status == STATUS_ENABLED]
        menu_ids = frozenset(
            int(menu.menu_id) for role in enabled_roles for menu in (role.menus or [])
        )
        permission_codes = frozenset(
            menu.permission
            for role in enabled_roles
            for menu in (role.menus or [])
            if menu.permission
        )
        agents_by_role = (
            await agent_authorization_service.grantable_agent_ids_by_role_ids(
                db,
                [int(role.role_id) for role in roles],
            )
        )
        agent_ids = frozenset(
            agent_id
            for role in enabled_roles
            for agent_id in agents_by_role.get(int(role.role_id), set())
        )

        resolution = await resolve_data_scope_for_roles(
            db,
            user=user,
            roles=enabled_roles,
            depts=depts,
        )
        if resolution.accessible_user_scope is None:
            accessible_user_ids = None
        else:
            materialized = {
                int(user_id)
                for user_id in (
                    await db.execute(resolution.accessible_user_scope)
                ).scalars()
            }
            materialized.discard(int(user.user_id))
            own_dept_ids = {
                int(dept.dept_id)
                for dept in (depts if depts is not None else (user.depts or []))
            }
            if resolution.include_self or (
                resolution.accessible_dept_ids is not None
                and bool(own_dept_ids & resolution.accessible_dept_ids)
            ):
                materialized.add(int(user.user_id))
            accessible_user_ids = frozenset(materialized)
        return RoleSetAuthority(
            permission_codes=permission_codes,
            menu_ids=menu_ids,
            agent_ids=agent_ids,
            accessible_dept_ids=resolution.accessible_dept_ids,
            accessible_user_ids=accessible_user_ids,
            role_definition_signature=self._role_definition_signature(
                roles,
                agents_by_role,
            ),
        )

    @staticmethod
    def _role_definition_signature(
        roles: list[Role],
        agents_by_role: dict[int, set[int]],
    ) -> tuple[
        tuple[
            int,
            str,
            str,
            str,
            tuple[int, ...],
            tuple[int, ...],
            tuple[int, ...],
        ],
        ...,
    ]:
        """Return stable role, menu, department, and Agent binding facts."""
        return tuple(
            sorted(
                (
                    int(role.role_id),
                    str(role.role_code),
                    str(role.status),
                    str(role.data_scope),
                    tuple(sorted(int(menu.menu_id) for menu in (role.menus or []))),
                    tuple(sorted(int(dept.dept_id) for dept in (role.depts or []))),
                    tuple(sorted(agents_by_role.get(int(role.role_id), set()))),
                )
                for role in roles
            )
        )

    async def materialize_role_set_authorities(
        self,
        db: AsyncSession,
        *,
        candidates: list[tuple[User, list[Role], list[Dept]]],
    ) -> list[RoleSetAuthority]:
        """Materialize many role sets with a bounded number of bulk queries."""
        from app.modules.ai.service.agent_authorization_service import (  # noqa: PLC0415
            agent_authorization_service,
        )

        enabled_role_sets = [
            [role for role in roles if role.status == STATUS_ENABLED]
            for _user, roles, _depts in candidates
        ]
        all_role_ids = {
            int(role.role_id) for _user, roles, _depts in candidates for role in roles
        }
        agents_by_role = (
            await agent_authorization_service.grantable_agent_ids_by_role_ids(
                db,
                all_role_ids,
            )
        )
        all_own_dept_ids = {
            int(dept.dept_id) for _user, _roles, depts in candidates for dept in depts
        }
        descendant_rows: list[tuple[int, str | None]] = []
        if all_own_dept_ids:
            descendant_rows = [
                (int(dept_id), ancestors)
                for dept_id, ancestors in (
                    await db.execute(
                        select(Dept.dept_id, Dept.ancestors).where(
                            or_(
                                Dept.dept_id.in_(all_own_dept_ids),
                                *(
                                    func.concat(",", Dept.ancestors, ",").like(
                                        f"%,{dept_id},%"
                                    )
                                    for dept_id in sorted(all_own_dept_ids)
                                ),
                            )
                        )
                    )
                ).all()
            ]

        resolved_dept_ids: list[frozenset[int] | None] = []
        include_self_values: list[bool] = []
        for (user, _roles, depts), enabled_roles in zip(
            candidates,
            enabled_role_sets,
            strict=True,
        ):
            if user.user_name == ADMIN_USERNAME or any(
                role.role_code == SUPER_ADMIN_ROLE_CODE for role in enabled_roles
            ):
                resolved_dept_ids.append(None)
                include_self_values.append(True)
                continue
            scope_kinds = {
                role.data_scope
                for role in enabled_roles
                if role.data_scope
                in {
                    DATA_SCOPE_ALL,
                    DATA_SCOPE_CUSTOM,
                    DATA_SCOPE_DEPT,
                    DATA_SCOPE_DEPT_AND_SUB,
                    DATA_SCOPE_SELF,
                }
            }
            if not scope_kinds:
                scope_kinds = {DATA_SCOPE_SELF}
            if DATA_SCOPE_ALL in scope_kinds:
                resolved_dept_ids.append(None)
                include_self_values.append(True)
                continue

            own_dept_ids = {int(dept.dept_id) for dept in depts}
            accessible_dept_ids: set[int] = set()
            include_self = DATA_SCOPE_SELF in scope_kinds
            if DATA_SCOPE_DEPT in scope_kinds:
                accessible_dept_ids.update(own_dept_ids)
                include_self = include_self or not own_dept_ids
            if DATA_SCOPE_DEPT_AND_SUB in scope_kinds:
                subtree_ids = {
                    dept_id
                    for dept_id, ancestors in descendant_rows
                    if dept_id in own_dept_ids
                    or bool(
                        own_dept_ids
                        & {
                            int(part)
                            for part in (ancestors or "").split(",")
                            if part.isdigit()
                        }
                    )
                }
                accessible_dept_ids.update(subtree_ids)
                include_self = include_self or not subtree_ids
            for role in enabled_roles:
                if role.data_scope != DATA_SCOPE_CUSTOM:
                    continue
                custom_ids = {
                    int(dept.dept_id)
                    for dept in (role.depts or [])
                    if dept.status == STATUS_ENABLED
                }
                accessible_dept_ids.update(custom_ids)
                include_self = include_self or not custom_ids
            resolved_dept_ids.append(frozenset(accessible_dept_ids))
            include_self_values.append(include_self)

        bounded_dept_ids = {
            dept_id
            for dept_ids in resolved_dept_ids
            if dept_ids is not None
            for dept_id in dept_ids
        }
        users_by_dept: dict[int, set[int]] = {
            dept_id: set() for dept_id in bounded_dept_ids
        }
        if bounded_dept_ids:
            rows = (
                await db.execute(
                    select(user_depts.c.dept_id, user_depts.c.user_id).where(
                        user_depts.c.dept_id.in_(bounded_dept_ids)
                    )
                )
            ).all()
            for dept_id, user_id in rows:
                users_by_dept[int(dept_id)].add(int(user_id))

        result: list[RoleSetAuthority] = []
        for (
            (user, _roles, depts),
            enabled_roles,
            accessible_dept_ids,
            include_self,
        ) in zip(
            candidates,
            enabled_role_sets,
            resolved_dept_ids,
            include_self_values,
            strict=True,
        ):
            menu_ids = frozenset(
                int(menu.menu_id)
                for role in enabled_roles
                for menu in (role.menus or [])
            )
            permission_codes = frozenset(
                menu.permission
                for role in enabled_roles
                for menu in (role.menus or [])
                if menu.permission
            )
            agent_ids = frozenset(
                agent_id
                for role in enabled_roles
                for agent_id in agents_by_role.get(int(role.role_id), set())
            )
            if accessible_dept_ids is None:
                accessible_user_ids = None
            else:
                materialized = {
                    user_id
                    for dept_id in accessible_dept_ids
                    for user_id in users_by_dept.get(dept_id, set())
                }
                target_user_id = int(user.user_id)
                materialized.discard(target_user_id)
                own_dept_ids = {int(dept.dept_id) for dept in depts}
                if include_self or bool(own_dept_ids & accessible_dept_ids):
                    materialized.add(target_user_id)
                accessible_user_ids = frozenset(materialized)
            result.append(
                RoleSetAuthority(
                    permission_codes=permission_codes,
                    menu_ids=menu_ids,
                    agent_ids=agent_ids,
                    accessible_dept_ids=accessible_dept_ids,
                    accessible_user_ids=accessible_user_ids,
                    role_definition_signature=self._role_definition_signature(
                        list(_roles),
                        agents_by_role,
                    ),
                )
            )
        return result

    @staticmethod
    def ensure_role_set_dominated(
        actor: GrantAuthority,
        candidate: RoleSetAuthority,
        *,
        ignored_user_ids: frozenset[int] = frozenset(),
    ) -> None:
        if actor.super_admin:
            return
        collections_allowed = (
            actor.allows_permission_codes(candidate.permission_codes)
            and actor.allows_menu_ids(candidate.menu_ids)
            and actor.allows_agent_ids(candidate.agent_ids)
        )
        if candidate.accessible_dept_ids is None:
            departments_allowed = actor.accessible_dept_ids is None
        else:
            departments_allowed = (
                actor.accessible_dept_ids is None
                or candidate.accessible_dept_ids <= actor.accessible_dept_ids
            )
        if candidate.accessible_user_ids is None:
            users_allowed = actor.accessible_user_scope is None
        else:
            candidate_user_ids = candidate.accessible_user_ids - ignored_user_ids
            users_allowed = (
                actor.accessible_user_scope is None
                or candidate_user_ids <= actor.accessible_user_scope
            )
        if collections_allowed and departments_allowed and users_allowed:
            return
        raise AuthorizationException(
            "角色授权超出当前操作者的授权上界",
            error_code="USER_ROLE_AUTHORITY_EXCEEDED",
        )

    @staticmethod
    def _ensure_target_protection(
        *,
        actor: GrantAuthority,
        actor_user_id: int,
        target: User,
        old_roles: list[Role],
        new_roles: list[Role],
        allow_empty_old_roles: bool,
    ) -> None:
        if actor_user_id == int(target.user_id):
            raise BusinessRuleException(
                "不能修改当前登录账号的角色",
                error_code="USER_ROLE_SELF_ASSIGNMENT_FORBIDDEN",
            )
        protected = target.user_name == ADMIN_USERNAME or any(
            role.status == STATUS_ENABLED and role.role_code == SUPER_ADMIN_ROLE_CODE
            for role in [*old_roles, *new_roles]
        )
        if protected and not actor.super_admin:
            raise AuthorizationException(
                "只有超级管理员可以修改超级管理员账号的角色",
                error_code="USER_ROLE_SUPER_ADMIN_REQUIRED",
            )
        if not allow_empty_old_roles and not old_roles:
            raise BusinessRuleException(
                "目标用户当前角色集合为空",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )

    @staticmethod
    def _ensure_target_visible(actor: GrantAuthority, target_user_id: int) -> None:
        if actor.super_admin or actor.accessible_user_scope is None:
            return
        if target_user_id in actor.accessible_user_scope:
            return
        raise AuthorizationException(
            "目标用户不在当前数据权限范围内",
            error_code="USER_ROLE_TARGET_OUT_OF_SCOPE",
        )

    @staticmethod
    def _ensure_created_target_visible(
        actor: GrantAuthority,
        prospective_dept_ids: frozenset[int],
    ) -> None:
        if actor.super_admin or actor.accessible_dept_ids is None:
            return
        if prospective_dept_ids and prospective_dept_ids <= actor.accessible_dept_ids:
            return
        raise AuthorizationException(
            "新用户不在当前数据权限范围内",
            error_code="USER_ROLE_TARGET_OUT_OF_SCOPE",
        )

    async def _authorize_role_set(
        self,
        db: AsyncSession,
        *,
        actor: GrantAuthority,
        actor_user_id: int,
        target: User,
        requested_roles: list[Role],
        prospective_depts: list[Dept] | None,
        allow_empty_old_roles: bool,
        created_target: bool,
        ignored_user_ids: frozenset[int] = frozenset(),
    ) -> None:
        old_roles = list(target.roles or [])
        self._ensure_target_protection(
            actor=actor,
            actor_user_id=actor_user_id,
            target=target,
            old_roles=old_roles,
            new_roles=requested_roles,
            allow_empty_old_roles=allow_empty_old_roles,
        )
        if created_target:
            self._ensure_created_target_visible(
                actor,
                frozenset(int(dept.dept_id) for dept in (prospective_depts or [])),
            )
            ignored_user_ids = ignored_user_ids | frozenset({int(target.user_id)})
        else:
            self._ensure_target_visible(actor, int(target.user_id))

        if old_roles:
            old_authority = await self.materialize_role_set_authority(
                db,
                user=target,
                roles=old_roles,
                depts=list(target.depts or []),
            )
            self.ensure_role_set_dominated(
                actor,
                old_authority,
                ignored_user_ids=ignored_user_ids,
            )
        new_authority = await self.materialize_role_set_authority(
            db,
            user=target,
            roles=requested_roles,
            depts=prospective_depts,
        )
        self.ensure_role_set_dominated(
            actor,
            new_authority,
            ignored_user_ids=ignored_user_ids,
        )

    async def _replace_roles(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        requested_roles: list[Role],
        required_permissions: frozenset[str],
        allow_empty_old_roles: bool,
        prospective_dept_ids: list[int] | None = None,
        ignored_user_ids: frozenset[int] = frozenset(),
    ) -> UserRoleAssignmentResult:
        actor_before = await self._load_user(db, actor_user_id)
        target_before = await self._load_user(db, target_user_id)
        prospective_depts = (
            await self._load_prospective_depts(db, prospective_dept_ids)
            if prospective_dept_ids is not None
            else None
        )
        if actor_user_id == target_user_id:
            raise BusinessRuleException(
                "不能修改当前登录账号的角色",
                error_code="USER_ROLE_SELF_ASSIGNMENT_FORBIDDEN",
            )

        actor_role_snapshot = _role_ids(actor_before.roles)
        target_role_snapshot = _role_ids(target_before.roles)
        actor_dept_snapshot = tuple(
            sorted(int(dept.dept_id) for dept in actor_before.depts)
        )
        target_dept_snapshot = tuple(
            sorted(int(dept.dept_id) for dept in target_before.depts)
        )
        all_roles = [
            *actor_before.roles,
            *target_before.roles,
            *requested_roles,
        ]
        await authorization_lock_service.lock_targets(
            db,
            role_ids=set(_role_ids(all_roles)),
            dept_ids=set(_dept_ids(actor_before, all_roles))
            | {int(dept.dept_id) for dept in target_before.depts}
            | {int(dept.dept_id) for dept in (prospective_depts or [])},
            user_ids={actor_user_id, target_user_id},
        )

        actor_user = await self._load_user(db, actor_user_id)
        target_user = await self._load_user(db, target_user_id)
        if (
            _role_ids(actor_user.roles) != actor_role_snapshot
            or _role_ids(target_user.roles) != target_role_snapshot
            or tuple(sorted(int(dept.dept_id) for dept in actor_user.depts))
            != actor_dept_snapshot
            or tuple(sorted(int(dept.dept_id) for dept in target_user.depts))
            != target_dept_snapshot
        ):
            raise BusinessRuleException(
                "授权事实已变化，请重试",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )

        live_requested_roles = await self._load_requested_roles(
            db,
            list(_role_ids(requested_roles)),
        )
        authority = await grant_authority_service.build(db, actor_user_id)
        self._require_permissions(authority, required_permissions)
        await self._authorize_role_set(
            db,
            actor=authority,
            actor_user_id=actor_user_id,
            target=target_user,
            requested_roles=live_requested_roles,
            prospective_depts=prospective_depts,
            allow_empty_old_roles=allow_empty_old_roles,
            created_target=allow_empty_old_roles,
            ignored_user_ids=ignored_user_ids,
        )

        old_role_ids = _role_ids(target_user.roles)
        target_user.roles = live_requested_roles
        await db.flush()
        return UserRoleAssignmentResult(
            user_id=target_user_id,
            old_role_ids=old_role_ids,
            new_role_ids=_role_ids(live_requested_roles),
        )

    async def replace_roles(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        role_ids: list[int | str],
    ) -> UserRoleAssignmentResult:
        """Replace one existing user's complete role set without committing."""
        requested_roles = await self._load_requested_roles(db, role_ids)
        return await self._replace_roles(
            db,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            requested_roles=requested_roles,
            required_permissions=frozenset(
                {USER_EDIT_PERMISSION, USER_ROLE_AUTH_PERMISSION}
            ),
            allow_empty_old_roles=False,
        )

    async def assign_created_user_roles(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        role_ids: list[int | str] | None,
        dept_ids: list[int],
    ) -> UserRoleAssignmentResult:
        """Assign explicit roles or the fixed default role to a newly created user."""
        explicit_roles = role_ids is not None
        await self.ensure_create_permissions(
            db,
            actor_user_id=actor_user_id,
            explicit_roles=explicit_roles,
        )
        requested_roles = (
            await self._load_requested_roles(db, role_ids or [])
            if explicit_roles
            else [await self._default_role(db)]
        )
        required = {USER_ADD_PERMISSION}
        if explicit_roles:
            required.add(USER_ROLE_AUTH_PERMISSION)
        return await self._replace_roles(
            db,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            requested_roles=requested_roles,
            required_permissions=frozenset(required),
            allow_empty_old_roles=True,
            prospective_dept_ids=dept_ids,
        )

    async def validate_import_role_assignment(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int | None,
        target_user_name: str,
        target_status: str,
        role_ids: list[int],
        dept_ids: list[int],
        authority: GrantAuthority | None = None,
        ignored_user_ids: frozenset[int] = frozenset(),
        prospective_user_id: int | None = None,
    ) -> tuple[int, ...]:
        """Validate one resolved import target without writing associations."""
        requested_roles = await self._load_requested_roles(db, role_ids)
        prospective_depts = await self._load_prospective_depts(db, dept_ids)
        created_target = target_user_id is None
        if created_target:
            target = User(
                user_id=prospective_user_id or next_id(),
                user_name=target_user_name,
                nickname=target_user_name,
                hashed_password="",
                status=target_status,
                roles=[],
                depts=[],
            )
        else:
            target = await self._load_user(db, target_user_id)
        live_authority = authority or await grant_authority_service.build(
            db,
            actor_user_id,
        )
        await self._authorize_role_set(
            db,
            actor=live_authority,
            actor_user_id=actor_user_id,
            target=target,
            requested_roles=requested_roles,
            prospective_depts=prospective_depts,
            allow_empty_old_roles=created_target,
            created_target=created_target,
            ignored_user_ids=ignored_user_ids,
        )
        return _role_ids(requested_roles)

    async def assign_imported_user_roles(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        role_ids: list[int | str] | None,
        dept_ids: list[int],
        has_role_column: bool,
        ignored_user_ids: frozenset[int] = frozenset(),
    ) -> UserRoleAssignmentResult:
        """Assign imported roles after revalidating the locked import authority."""
        requested_roles = (
            await self._load_requested_roles(db, role_ids)
            if role_ids is not None
            else [await self._default_role(db)]
        )
        required = {USER_IMPORT_PERMISSION}
        if has_role_column:
            required.add(USER_ROLE_AUTH_PERMISSION)
        return await self._replace_roles(
            db,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            requested_roles=requested_roles,
            required_permissions=frozenset(required),
            allow_empty_old_roles=True,
            prospective_dept_ids=dept_ids,
            ignored_user_ids=ignored_user_ids,
        )

    async def apply_locked_import_roles(
        self,
        db: AsyncSession,
        *,
        target_user_id: int,
        role_ids: list[int | str],
    ) -> UserRoleAssignmentResult:
        """Apply a set already validated under the import batch authorization lock."""
        requested_roles = await self._load_requested_roles(db, role_ids)
        target = await self._load_user(db, target_user_id)
        old_role_ids = _role_ids(list(target.roles or []))
        target.roles = requested_roles
        await db.flush()
        return UserRoleAssignmentResult(
            user_id=target_user_id,
            old_role_ids=old_role_ids,
            new_role_ids=_role_ids(requested_roles),
        )

    async def lock_import_targets(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        target_user_ids: set[int],
        role_ids: set[int],
        dept_ids: set[int],
    ) -> User:
        """Prelock one import batch before any authorization association is written."""
        actor_before = await self._load_user(db, actor_user_id)
        targets_before = [
            await self._load_user(db, target_user_id)
            for target_user_id in sorted(target_user_ids)
        ]
        requested_roles = (
            await self._load_requested_roles(db, sorted(role_ids)) if role_ids else []
        )
        actor_role_snapshot = _role_ids(actor_before.roles)
        actor_dept_snapshot = tuple(
            sorted(int(dept.dept_id) for dept in actor_before.depts)
        )
        target_snapshots = {
            int(target.user_id): (
                _role_ids(target.roles),
                tuple(sorted(int(dept.dept_id) for dept in target.depts)),
            )
            for target in targets_before
        }
        all_roles = [
            *actor_before.roles,
            *(role for target in targets_before for role in target.roles),
            *requested_roles,
        ]
        all_dept_ids = {
            *dept_ids,
            *_dept_ids(actor_before, all_roles),
            *(int(dept.dept_id) for target in targets_before for dept in target.depts),
        }
        if all_dept_ids and any(
            role.status == STATUS_ENABLED and role.data_scope == DATA_SCOPE_DEPT_AND_SUB
            for role in all_roles
        ):
            all_dept_ids.update(await get_dept_and_sub_ids(db, sorted(all_dept_ids)))
        await authorization_lock_service.lock_targets(
            db,
            role_ids=set(_role_ids(all_roles)),
            dept_ids=all_dept_ids,
            user_ids={actor_user_id, *target_user_ids},
        )

        actor = await self._load_user(db, actor_user_id)
        if (
            _role_ids(actor.roles) != actor_role_snapshot
            or tuple(sorted(int(dept.dept_id) for dept in actor.depts))
            != actor_dept_snapshot
        ):
            raise BusinessRuleException(
                "授权事实已变化，请重试",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        for target_user_id, (
            expected_roles,
            expected_depts,
        ) in target_snapshots.items():
            target = await self._load_user(db, target_user_id)
            if (
                _role_ids(target.roles) != expected_roles
                or tuple(sorted(int(dept.dept_id) for dept in target.depts))
                != expected_depts
            ):
                raise BusinessRuleException(
                    "授权事实已变化，请重试",
                    error_code="AUTHORIZATION_SNAPSHOT_STALE",
                )
        return actor

    async def list_assignable_roles(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        query: str | None,
        limit: int,
    ) -> list[Role]:
        """Return minimal role candidates dominated by the actor's role template."""
        from app.modules.ai.service.agent_authorization_service import (  # noqa: PLC0415
            agent_authorization_service,
        )

        authority = await grant_authority_service.build(db, actor_user_id)
        self._require_permissions(authority, frozenset({USER_ROLE_AUTH_PERMISSION}))
        statement = (
            select(Role)
            .where(Role.status == STATUS_ENABLED)
            .options(selectinload(Role.menus), selectinload(Role.depts))
            .order_by(Role.role_code, Role.role_id)
        )
        normalized_query = (query or "").strip()
        if normalized_query:
            pattern = f"%{normalized_query}%"
            statement = statement.where(
                or_(Role.role_code.ilike(pattern), Role.role_name.ilike(pattern))
            )
        roles = list((await db.execute(statement)).unique().scalars().all())
        role_ids = [int(role.role_id) for role in roles]
        bindings = await agent_authorization_service.grantable_agent_ids_by_role_ids(
            db,
            role_ids,
        )

        result: list[Role] = []
        for role in roles:
            if role.role_code == SUPER_ADMIN_ROLE_CODE and not authority.super_admin:
                continue
            permission_codes = {
                menu.permission for menu in role.menus if menu.permission
            }
            menu_ids = {int(menu.menu_id) for menu in role.menus}
            custom_dept_ids = {int(dept.dept_id) for dept in role.depts}
            if not (
                authority.allows_permission_codes(permission_codes)
                and authority.allows_menu_ids(menu_ids)
                and authority.allows_agent_ids(bindings[int(role.role_id)])
                and authority.allows_scope_kind(role.data_scope, custom_dept_ids)
            ):
                continue
            result.append(role)
            if len(result) >= limit:
                break
        return result


user_role_assignment_service = UserRoleAssignmentService()
