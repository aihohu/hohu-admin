"""Shared Role delegation policy for authorization aggregate writers."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import ADMIN_USERNAME, STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    NotFoundException,
)
from app.db.base import user_roles
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import authorization_lock_service
from app.modules.system.service.authorization_snapshot import (
    materialized_role_set_snapshot,
)
from app.modules.system.service.grant_authority import (
    GrantAuthority,
    grant_authority_service,
)
from app.modules.system.service.user_role_assignment_service import (
    RoleSetAuthority,
    user_role_assignment_service,
)

ROLE_AGENT_PERMISSION = "system:role:ai-agent-auth"
PROTECTED_ROLE_CODES = frozenset({SUPER_ADMIN_ROLE_CODE})


@dataclass(frozen=True)
class RoleAgentDelegationPlan:
    """Stable identifiers authorized and locked for one binding replacement."""

    role_id: int
    old_agent_ids: tuple[int, ...]
    new_agent_ids: tuple[int, ...]
    member_user_ids: tuple[int, ...]


@dataclass(frozen=True)
class _RoleAgentEvaluation:
    plan: RoleAgentDelegationPlan
    role_ids: tuple[int, ...]
    dept_ids: tuple[int, ...]
    user_ids: tuple[int, ...]
    snapshot: dict[str, Any]


class RoleDelegationService:
    """Authorize Role mutations against actor and affected-member authority."""

    @staticmethod
    def _require_entry_permission(authority: GrantAuthority) -> None:
        if authority.allows_permission_codes({ROLE_AGENT_PERMISSION}):
            return
        raise AuthorizationException(
            "权限不足",
            error_code="MISSING_PERMISSION",
        )

    @staticmethod
    def _raise_authority_exceeded() -> None:
        raise AuthorizationException(
            "角色 Agent 授权超出当前操作者的授权上界",
            error_code="AI_ROLE_AGENT_AUTHORITY_EXCEEDED",
        )

    @staticmethod
    def _raise_global_impact() -> None:
        raise AuthorizationException(
            "角色变更影响了当前操作者无权管理的成员",
            error_code="AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE",
        )

    @staticmethod
    async def _load_user(db: AsyncSession, user_id: int) -> User:
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

    @staticmethod
    async def _load_role(db: AsyncSession, role_id: int) -> Role:
        role = await db.scalar(
            select(Role)
            .where(Role.role_id == role_id)
            .options(selectinload(Role.menus), selectinload(Role.depts))
            .execution_options(populate_existing=True)
        )
        if role is None:
            raise NotFoundException("Role", error_code="AI_ROLE_NOT_FOUND")
        return role

    @staticmethod
    async def _load_members(db: AsyncSession, role_id: int) -> list[User]:
        return list(
            (
                await db.execute(
                    select(User)
                    .join(user_roles, user_roles.c.user_id == User.user_id)
                    .where(user_roles.c.role_id == role_id)
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

    @staticmethod
    async def _active_agent_ids(db: AsyncSession, role_id: int) -> tuple[int, ...]:
        from app.modules.ai.service.agent_authorization_service import (  # noqa: PLC0415
            agent_authorization_service,
        )

        by_role = await agent_authorization_service.grantable_agent_ids_by_role_ids(
            db,
            [role_id],
        )
        return tuple(sorted(by_role.get(role_id, set())))

    @staticmethod
    def _role_ids(users: list[User], target_role_id: int) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    target_role_id,
                    *(
                        int(role.role_id)
                        for user in users
                        for role in (user.roles or [])
                    ),
                }
            )
        )

    @staticmethod
    def _dept_ids(role: Role, users: list[User]) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    *(int(dept.dept_id) for dept in (role.depts or [])),
                    *(
                        int(dept.dept_id)
                        for user in users
                        for dept in (user.depts or [])
                    ),
                    *(
                        int(dept.dept_id)
                        for user in users
                        for user_role in (user.roles or [])
                        for dept in (user_role.depts or [])
                    ),
                }
            )
        )

    def _ensure_role_definition_dominated(
        self,
        *,
        authority: GrantAuthority,
        role: Role,
        old_agent_ids: tuple[int, ...],
        new_agent_ids: tuple[int, ...],
    ) -> None:
        if authority.super_admin:
            return
        permission_codes = {
            menu.permission for menu in (role.menus or []) if menu.permission
        }
        menu_ids = {int(menu.menu_id) for menu in (role.menus or [])}
        custom_dept_ids = {int(dept.dept_id) for dept in (role.depts or [])}
        agent_ids = {*old_agent_ids, *new_agent_ids}
        if (
            authority.allows_permission_codes(permission_codes)
            and authority.allows_menu_ids(menu_ids)
            and authority.allows_agent_ids(agent_ids)
            and authority.allows_scope_kind(role.data_scope, custom_dept_ids)
        ):
            return
        self._raise_authority_exceeded()

    def _ensure_member_protection(
        self,
        *,
        authority: GrantAuthority,
        actor_user_id: int,
        role: Role,
        members: list[User],
    ) -> None:
        if authority.super_admin:
            return
        if role.role_code in PROTECTED_ROLE_CODES:
            raise AuthorizationException(
                "保护角色只能由超级管理员修改",
                error_code="AI_ROLE_PROTECTED",
            )
        member_ids = {int(member.user_id) for member in members}
        if actor_user_id in member_ids:
            raise AuthorizationException(
                "不能修改当前操作者所属角色的授权字段",
                error_code="AI_ROLE_SELF_MUTATION_FORBIDDEN",
            )
        if authority.accessible_user_scope is not None and not member_ids <= set(
            authority.accessible_user_scope
        ):
            self._raise_global_impact()
        protected_member = any(
            member.user_name == ADMIN_USERNAME
            or any(
                member_role.status == STATUS_ENABLED
                and member_role.role_code in PROTECTED_ROLE_CODES
                for member_role in (member.roles or [])
            )
            for member in members
        )
        if protected_member:
            self._raise_global_impact()

    def _ensure_member_authorities_dominated(
        self,
        *,
        authority: GrantAuthority,
        before: list[RoleSetAuthority],
        after: list[RoleSetAuthority],
    ) -> None:
        if authority.super_admin:
            return
        try:
            for value in [*before, *after]:
                user_role_assignment_service.ensure_role_set_dominated(
                    authority,
                    value,
                )
        except AuthorizationException:
            self._raise_global_impact()

    def _build_evaluation(
        self,
        *,
        authority: GrantAuthority,
        actor: User,
        role: Role,
        members: list[User],
        old_agent_ids: tuple[int, ...],
        new_agent_ids: tuple[int, ...],
        before: list[RoleSetAuthority],
        after: list[RoleSetAuthority],
    ) -> _RoleAgentEvaluation:
        users_for_lock = [actor, *members]
        role_ids = self._role_ids(users_for_lock, int(role.role_id))
        dept_ids = self._dept_ids(role, users_for_lock)
        member_ids = tuple(sorted(int(member.user_id) for member in members))
        plan = RoleAgentDelegationPlan(
            role_id=int(role.role_id),
            old_agent_ids=old_agent_ids,
            new_agent_ids=new_agent_ids,
            member_user_ids=member_ids,
        )
        snapshot = {
            "actorAuthorityVersion": authority.version_summary,
            "targetRole": {
                "roleId": str(role.role_id),
                "roleCode": role.role_code,
                "status": role.status,
                "dataScope": role.data_scope,
                "menuIds": sorted(int(menu.menu_id) for menu in (role.menus or [])),
                "customDeptIds": sorted(
                    int(dept.dept_id) for dept in (role.depts or [])
                ),
                "oldAgentIds": list(old_agent_ids),
                "newAgentIds": list(new_agent_ids),
            },
            "members": [
                {
                    "userId": str(member.user_id),
                    "userName": member.user_name,
                    "status": member.status,
                    "roleIds": sorted(
                        int(member_role.role_id) for member_role in (member.roles or [])
                    ),
                    "deptIds": sorted(
                        int(dept.dept_id) for dept in (member.depts or [])
                    ),
                    "before": materialized_role_set_snapshot(before[index]),
                    "after": materialized_role_set_snapshot(after[index]),
                }
                for index, member in enumerate(members)
            ],
        }
        return _RoleAgentEvaluation(
            plan=plan,
            role_ids=role_ids,
            dept_ids=dept_ids,
            user_ids=tuple(sorted({int(actor.user_id), *member_ids})),
            snapshot=snapshot,
        )

    async def _evaluate_agent_replacement(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        role_id: int,
        new_agent_ids: tuple[int, ...],
    ) -> _RoleAgentEvaluation:
        actor = await self._load_user(db, actor_user_id)
        authority = await grant_authority_service.build(db, actor_user_id)
        self._require_entry_permission(authority)
        role = await self._load_role(db, role_id)
        old_agent_ids = await self._active_agent_ids(db, role_id)

        if authority.super_admin:
            return self._build_evaluation(
                authority=authority,
                actor=actor,
                role=role,
                members=[],
                old_agent_ids=old_agent_ids,
                new_agent_ids=new_agent_ids,
                before=[],
                after=[],
            )

        members = await self._load_members(db, role_id)

        self._ensure_member_protection(
            authority=authority,
            actor_user_id=actor_user_id,
            role=role,
            members=members,
        )
        self._ensure_role_definition_dominated(
            authority=authority,
            role=role,
            old_agent_ids=old_agent_ids,
            new_agent_ids=new_agent_ids,
        )

        candidates = [
            (member, list(member.roles or []), list(member.depts or []))
            for member in members
        ]
        before = await user_role_assignment_service.materialize_role_set_authorities(
            db,
            candidates=candidates,
        )
        after = await user_role_assignment_service.materialize_role_set_authorities(
            db,
            candidates=candidates,
            agent_ids_by_role_override={role_id: set(new_agent_ids)},
        )
        self._ensure_member_authorities_dominated(
            authority=authority,
            before=before,
            after=after,
        )

        return self._build_evaluation(
            authority=authority,
            actor=actor,
            role=role,
            members=members,
            old_agent_ids=old_agent_ids,
            new_agent_ids=new_agent_ids,
            before=before,
            after=after,
        )

    async def authorize_and_lock_agent_replacement(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        role_id: int,
        new_agent_ids: list[int] | tuple[int, ...],
    ) -> RoleAgentDelegationPlan:
        """Authorize, lock, and re-evaluate one complete Role-Agent set."""
        normalized_new_ids = tuple(sorted(int(agent_id) for agent_id in new_agent_ids))
        initial = await self._evaluate_agent_replacement(
            db,
            actor_user_id=actor_user_id,
            role_id=role_id,
            new_agent_ids=normalized_new_ids,
        )
        await authorization_lock_service.lock_targets(
            db,
            role_ids=initial.role_ids,
            dept_ids=initial.dept_ids,
            user_ids=initial.user_ids,
        )
        try:
            locked = await self._evaluate_agent_replacement(
                db,
                actor_user_id=actor_user_id,
                role_id=role_id,
                new_agent_ids=normalized_new_ids,
            )
        except BusinessException as exc:
            raise BusinessRuleException(
                "授权事实已变化，请重新提交",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            ) from exc
        if (
            locked.role_ids != initial.role_ids
            or locked.dept_ids != initial.dept_ids
            or locked.user_ids != initial.user_ids
            or locked.snapshot != initial.snapshot
        ):
            raise BusinessRuleException(
                "授权事实已变化，请重新提交",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        return locked.plan


role_delegation_service = RoleDelegationService()
