"""Shared delegated Role management policy for page APIs and AI tools."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    ADMIN_USERNAME,
    DATA_SCOPE_CUSTOM,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.schemas.role_agent import RoleAgentBindReq
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.role import RoleCreate, RoleUpdate
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

PROTECTED_ROLE_CODES = frozenset({SUPER_ADMIN_ROLE_CODE})


@dataclass(frozen=True)
class RoleManagementPreview:
    """Server-owned preview for one Role aggregate mutation."""

    action: str
    role_id: int | None
    member_user_ids: tuple[int, ...]
    snapshot: dict[str, Any]
    target_role_name: str | None = None


@dataclass(frozen=True)
class RoleSummary:
    """Minimal Role metadata plus a current delegation assessment."""

    role_id: int
    role_code: str
    role_name: str
    status: str
    data_scope: str
    delegable: bool
    blocked_reason_code: str | None


@dataclass(frozen=True)
class _RoleEvaluation:
    preview: RoleManagementPreview
    role_ids: tuple[int, ...]
    dept_ids: tuple[int, ...]
    user_ids: tuple[int, ...]
    role: Role | None
    candidate_menus: tuple[Menu, ...]
    candidate_depts: tuple[Dept, ...]


class RoleManagementService:
    """Authorize Role definitions and complete related capability sets."""

    @staticmethod
    def _require_permission(authority: GrantAuthority, permission: str) -> None:
        if authority.allows_permission_codes({permission}):
            return
        raise AuthorizationException(
            "缺少角色管理权限",
            error_code="PERMISSION_DENIED",
        )

    @staticmethod
    def _raise_authority_exceeded() -> None:
        raise AuthorizationException(
            "角色定义超出当前操作者授权上界",
            error_code="AI_ROLE_AUTHORITY_EXCEEDED",
        )

    @staticmethod
    def _raise_global_impact() -> None:
        raise AuthorizationException(
            "角色变更影响了范围外成员或授权",
            error_code="AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE",
        )

    @staticmethod
    def _reject_irrelevant_custom_depts(
        *,
        data_scope: str,
        dept_ids: list[int] | None,
    ) -> None:
        if data_scope == DATA_SCOPE_CUSTOM or not dept_ids:
            return
        raise BusinessRuleException(
            "仅自定义数据范围可以关联部门",
            error_code="ROLE_DEPTS_REQUIRE_CUSTOM_SCOPE",
        )

    async def _load_role(self, db: AsyncSession, role_id: int) -> Role:
        role = await db.scalar(
            select(Role)
            .where(Role.role_id == role_id)
            .options(selectinload(Role.menus), selectinload(Role.depts))
            .execution_options(populate_existing=True)
        )
        if role is None:
            raise NotFoundException("角色")
        return role

    async def _load_role_members(
        self,
        db: AsyncSession,
        role_id: int,
    ) -> list[User]:
        users = await db.execute(
            select(User)
            .join(User.roles)
            .where(Role.role_id == role_id)
            .options(
                selectinload(User.depts),
                selectinload(User.roles).selectinload(Role.menus),
                selectinload(User.roles).selectinload(Role.depts),
            )
            .order_by(User.user_id)
            .execution_options(populate_existing=True)
        )
        return list(users.scalars().unique())

    async def _load_actor(self, db: AsyncSession, actor_user_id: int) -> User:
        actor = await db.scalar(
            select(User)
            .where(User.user_id == actor_user_id)
            .options(
                selectinload(User.roles).selectinload(Role.menus),
                selectinload(User.roles).selectinload(Role.depts),
                selectinload(User.depts),
            )
            .execution_options(populate_existing=True)
        )
        if actor is None:
            raise NotFoundException("用户")
        return actor

    @staticmethod
    def _role_agent_ids(rows: list[tuple[int, int]]) -> dict[int, set[int]]:
        result: dict[int, set[int]] = {}
        for role_id, agent_id in rows:
            result.setdefault(int(role_id), set()).add(int(agent_id))
        return result

    async def _active_agent_ids_by_role(
        self,
        db: AsyncSession,
        role_ids: set[int],
    ) -> dict[int, set[int]]:
        if not role_ids:
            return {}
        rows = (
            await db.execute(
                select(RoleAiAgent.role_id, RoleAiAgent.agent_id).where(
                    RoleAiAgent.role_id.in_(role_ids),
                    RoleAiAgent.enabled.is_(True),
                )
            )
        ).all()
        return self._role_agent_ids(rows)

    @staticmethod
    def _definition_dominated(
        authority: GrantAuthority,
        *,
        menus: list[Menu] | tuple[Menu, ...],
        depts: list[Dept] | tuple[Dept, ...],
        data_scope: str,
        agent_ids: set[int],
    ) -> bool:
        permission_codes = {menu.permission for menu in menus if menu.permission}
        menu_ids = {int(menu.menu_id) for menu in menus}
        dept_ids = {int(dept.dept_id) for dept in depts}
        return (
            authority.allows_permission_codes(permission_codes)
            and authority.allows_menu_ids(menu_ids)
            and authority.allows_agent_ids(agent_ids)
            and authority.allows_scope_kind(data_scope, dept_ids)
        )

    @staticmethod
    def _ensure_member_boundary(
        authority: GrantAuthority,
        *,
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
                "不能修改当前操作者所属角色",
                error_code="AI_ROLE_SELF_MUTATION_FORBIDDEN",
            )
        if authority.accessible_user_scope is not None and not member_ids <= set(
            authority.accessible_user_scope
        ):
            RoleManagementService._raise_global_impact()
        if any(
            member.user_name == ADMIN_USERNAME
            or any(
                member_role.status == STATUS_ENABLED
                and member_role.role_code in PROTECTED_ROLE_CODES
                for member_role in member.roles
            )
            for member in members
        ):
            RoleManagementService._raise_global_impact()

    @staticmethod
    def _ensure_materialized_dominated(
        authority: GrantAuthority,
        values: list[RoleSetAuthority],
    ) -> None:
        if authority.super_admin:
            return
        try:
            for value in values:
                user_role_assignment_service.ensure_role_set_dominated(
                    authority,
                    value,
                )
        except AuthorizationException:
            RoleManagementService._raise_global_impact()

    async def _load_depts(
        self,
        db: AsyncSession,
        dept_ids: list[int] | None,
    ) -> tuple[Dept, ...]:
        normalized = tuple(sorted({int(value) for value in (dept_ids or [])}))
        if not normalized:
            return ()
        depts = tuple(
            (
                await db.execute(
                    select(Dept)
                    .where(Dept.dept_id.in_(normalized))
                    .order_by(Dept.dept_id)
                )
            ).scalars()
        )
        if tuple(int(dept.dept_id) for dept in depts) != normalized:
            raise BusinessRuleException(
                "角色自定义部门不存在",
                error_code="ROLE_DEPT_NOT_FOUND",
            )
        return depts

    async def _expand_menus(
        self,
        db: AsyncSession,
        menu_ids: list[int] | None,
    ) -> tuple[Menu, ...]:
        normalized = {int(value) for value in (menu_ids or [])}
        if not normalized:
            return ()
        all_menus = list((await db.execute(select(Menu))).scalars())
        by_id = {int(menu.menu_id): menu for menu in all_menus}
        if not normalized <= set(by_id):
            raise BusinessRuleException(
                "菜单不存在",
                error_code="ROLE_MENU_NOT_FOUND",
            )
        expanded = set(normalized)
        for menu_id in tuple(normalized):
            current = by_id[menu_id]
            visited: set[int] = set()
            while current.parent_id is not None:
                parent_id = int(current.parent_id)
                if parent_id in visited or parent_id not in by_id:
                    break
                visited.add(parent_id)
                expanded.add(parent_id)
                current = by_id[parent_id]
        return tuple(by_id[value] for value in sorted(expanded))

    @staticmethod
    def _candidate_role(
        role: Role,
        *,
        values: dict[str, Any],
        menus: tuple[Menu, ...],
        depts: tuple[Dept, ...],
    ) -> Any:
        return SimpleNamespace(
            role_id=int(role.role_id),
            role_code=role.role_code,
            role_name=values.get("role_name", role.role_name),
            role_desc=values.get("role_desc", role.role_desc),
            status=values.get("status", role.status),
            data_scope=values.get("data_scope", role.data_scope),
            menus=list(menus),
            depts=list(depts),
        )

    async def _evaluate(
        self,
        db: AsyncSession,
        *,
        action: Literal["create", "update", "update_menus"],
        actor_user_id: int,
        role_in: RoleCreate | RoleUpdate | None = None,
        role_id: int | None = None,
        menu_ids: list[int] | None = None,
    ) -> _RoleEvaluation:
        permission = {
            "create": "system:role:add",
            "update": "system:role:edit",
            "update_menus": "system:role:menu-auth",
        }[action]
        authority = await grant_authority_service.build(db, actor_user_id)
        self._require_permission(authority, permission)
        actor = await self._load_actor(db, actor_user_id)
        role: Role | None = None
        members: list[User] = []
        before: list[RoleSetAuthority] = []
        after: list[RoleSetAuthority] = []
        candidate_menus: tuple[Menu, ...] = ()
        candidate_depts: tuple[Dept, ...] = ()
        candidate: Any | None = None
        current_agent_ids: set[int] = set()

        if action == "create":
            assert isinstance(role_in, RoleCreate)
            if role_in.role_code in PROTECTED_ROLE_CODES and not authority.super_admin:
                raise AuthorizationException(
                    "不能创建保护角色",
                    error_code="AI_ROLE_PROTECTED",
                )
            duplicate = await db.scalar(
                select(Role.role_id).where(
                    or_(
                        Role.role_code == role_in.role_code,
                        Role.role_name == role_in.role_name,
                    )
                )
            )
            if duplicate is not None:
                raise DuplicateException("角色编码或名称", role_in.role_code)
            self._reject_irrelevant_custom_depts(
                data_scope=role_in.data_scope,
                dept_ids=role_in.dept_ids,
            )
            candidate_depts = await self._load_depts(db, role_in.dept_ids)
            if not self._definition_dominated(
                authority,
                menus=(),
                depts=candidate_depts,
                data_scope=role_in.data_scope,
                agent_ids=set(),
            ):
                self._raise_authority_exceeded()
            candidate_values = role_in.model_dump(mode="json")
        else:
            assert role_id is not None
            role = await self._load_role(db, role_id)
            members = await self._load_role_members(db, role_id)
            self._ensure_member_boundary(
                authority,
                actor_user_id=actor_user_id,
                role=role,
                members=members,
            )
            agent_ids_by_role = await self._active_agent_ids_by_role(
                db,
                {int(role.role_id)},
            )
            current_agent_ids = agent_ids_by_role.get(int(role.role_id), set())
            if not self._definition_dominated(
                authority,
                menus=role.menus,
                depts=role.depts,
                data_scope=role.data_scope,
                agent_ids=current_agent_ids,
            ):
                self._raise_authority_exceeded()
            if action == "update":
                assert isinstance(role_in, RoleUpdate)
                candidate_values = role_in.model_dump(exclude_unset=True)
                if "role_name" in candidate_values:
                    duplicate = await db.scalar(
                        select(Role.role_id).where(
                            Role.role_name == candidate_values["role_name"],
                            Role.role_id != role_id,
                        )
                    )
                    if duplicate is not None:
                        raise DuplicateException(
                            "角色名称",
                            str(candidate_values["role_name"]),
                        )
                next_scope = str(candidate_values.get("data_scope", role.data_scope))
                requested_dept_ids = candidate_values.get("dept_ids")
                self._reject_irrelevant_custom_depts(
                    data_scope=next_scope,
                    dept_ids=requested_dept_ids,
                )
                if next_scope != DATA_SCOPE_CUSTOM:
                    candidate_depts = ()
                elif "dept_ids" in candidate_values:
                    candidate_depts = await self._load_depts(
                        db,
                        requested_dept_ids,
                    )
                elif next_scope == role.data_scope:
                    candidate_depts = tuple(role.depts)
                else:
                    candidate_depts = ()
                candidate_menus = tuple(role.menus)
            else:
                candidate_values = {}
                candidate_depts = tuple(role.depts)
                candidate_menus = await self._expand_menus(db, menu_ids)
            candidate = self._candidate_role(
                role,
                values=candidate_values,
                menus=candidate_menus,
                depts=candidate_depts,
            )
            if not self._definition_dominated(
                authority,
                menus=candidate.menus,
                depts=candidate.depts,
                data_scope=candidate.data_scope,
                agent_ids=current_agent_ids,
            ):
                self._raise_authority_exceeded()
            candidates_before = [
                (member, list(member.roles), list(member.depts)) for member in members
            ]
            candidates_after = [
                (
                    member,
                    [
                        candidate if int(value.role_id) == role_id else value
                        for value in member.roles
                    ],
                    list(member.depts),
                )
                for member in members
            ]
            before = (
                await user_role_assignment_service.materialize_role_set_authorities(
                    db,
                    candidates=candidates_before,
                )
            )
            after = await user_role_assignment_service.materialize_role_set_authorities(
                db,
                candidates=candidates_after,
                agent_ids_by_role_override={role_id: current_agent_ids},
            )
            self._ensure_materialized_dominated(authority, [*before, *after])

        users_for_lock = [actor, *members]
        role_ids = {
            *authority.enabled_role_ids,
            *(int(value.role_id) for user in users_for_lock for value in user.roles),
            *({role_id} if role_id is not None else set()),
        }
        dept_ids = {
            *(int(dept.dept_id) for dept in candidate_depts),
            *(int(dept.dept_id) for user in users_for_lock for dept in user.depts),
            *(
                int(dept.dept_id)
                for user in users_for_lock
                for value in user.roles
                for dept in value.depts
            ),
        }
        for materialized in [*before, *after]:
            if materialized.accessible_dept_ids is not None:
                dept_ids.update(materialized.accessible_dept_ids)
        user_ids = {actor_user_id, *(int(member.user_id) for member in members)}
        if action == "create":
            target_definition = candidate_values
        else:
            assert role is not None
            assert candidate is not None
            target_definition = {
                "roleId": str(role_id),
                "current": {
                    "roleCode": role.role_code,
                    "roleName": role.role_name,
                    "roleDesc": role.role_desc,
                    "status": role.status,
                    "dataScope": role.data_scope,
                    "menuIds": sorted(int(menu.menu_id) for menu in role.menus),
                    "deptIds": sorted(int(dept.dept_id) for dept in role.depts),
                    "agentIds": sorted(current_agent_ids),
                },
                "candidate": {
                    "roleCode": candidate.role_code,
                    "roleName": candidate.role_name,
                    "roleDesc": candidate.role_desc,
                    "status": candidate.status,
                    "dataScope": candidate.data_scope,
                    "menuIds": sorted(int(menu.menu_id) for menu in candidate.menus),
                    "deptIds": sorted(int(dept.dept_id) for dept in candidate.depts),
                    "agentIds": sorted(current_agent_ids),
                },
            }
        snapshot = {
            "version": "phase3-role-write/v1",
            "action": action,
            "actorAuthorityVersion": authority.version_summary,
            "target": target_definition,
            "roleIds": sorted(role_ids),
            "deptIds": sorted(dept_ids),
            "userIds": sorted(user_ids),
            "members": [
                {
                    "userId": str(member.user_id),
                    "roleIds": sorted(int(value.role_id) for value in member.roles),
                    "deptIds": sorted(int(dept.dept_id) for dept in member.depts),
                    "before": materialized_role_set_snapshot(before[index]),
                    "after": materialized_role_set_snapshot(after[index]),
                }
                for index, member in enumerate(members)
            ],
        }
        return _RoleEvaluation(
            preview=RoleManagementPreview(
                action=action,
                role_id=role_id,
                member_user_ids=tuple(sorted(int(value.user_id) for value in members)),
                snapshot=snapshot,
                target_role_name=role.role_name if role is not None else None,
            ),
            role_ids=tuple(sorted(role_ids)),
            dept_ids=tuple(sorted(dept_ids)),
            user_ids=tuple(sorted(user_ids)),
            role=role,
            candidate_menus=candidate_menus,
            candidate_depts=candidate_depts,
        )

    async def _authorize_and_lock(
        self,
        db: AsyncSession,
        *,
        action: Literal["create", "update", "update_menus"],
        actor_user_id: int,
        role_in: RoleCreate | RoleUpdate | None = None,
        role_id: int | None = None,
        menu_ids: list[int] | None = None,
        expected_snapshot: dict[str, Any] | None,
    ) -> _RoleEvaluation:
        initial = await self._evaluate(
            db,
            action=action,
            actor_user_id=actor_user_id,
            role_in=role_in,
            role_id=role_id,
            menu_ids=menu_ids,
        )
        await authorization_lock_service.lock_targets(
            db,
            role_ids=initial.role_ids,
            dept_ids=initial.dept_ids,
            user_ids=initial.user_ids,
        )
        try:
            locked = await self._evaluate(
                db,
                action=action,
                actor_user_id=actor_user_id,
                role_in=role_in,
                role_id=role_id,
                menu_ids=menu_ids,
            )
        except BusinessException as exc:
            raise BusinessRuleException(
                "角色授权事实已变化",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            ) from exc
        if (
            initial.preview.snapshot != locked.preview.snapshot
            or initial.role_ids != locked.role_ids
            or initial.dept_ids != locked.dept_ids
            or initial.user_ids != locked.user_ids
        ):
            raise BusinessRuleException(
                "角色授权事实已变化",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        if (
            expected_snapshot is not None
            and locked.preview.snapshot != expected_snapshot
        ):
            raise BusinessRuleException(
                "角色审批快照已变化",
                error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
            )
        return locked

    async def preview_create(
        self,
        db: AsyncSession,
        role_in: RoleCreate,
        *,
        actor_user_id: int,
    ) -> RoleManagementPreview:
        return (
            await self._evaluate(
                db,
                action="create",
                actor_user_id=actor_user_id,
                role_in=role_in,
            )
        ).preview

    async def create(
        self,
        db: AsyncSession,
        role_in: RoleCreate,
        *,
        actor_user_id: int,
        expected_snapshot: dict[str, Any] | None = None,
    ) -> Role:
        evaluation = await self._authorize_and_lock(
            db,
            action="create",
            actor_user_id=actor_user_id,
            role_in=role_in,
            expected_snapshot=expected_snapshot,
        )
        role = Role(
            role_name=role_in.role_name,
            role_code=role_in.role_code,
            role_desc=role_in.role_desc,
            data_scope=role_in.data_scope,
            status=role_in.status,
            depts=list(evaluation.candidate_depts),
        )
        db.add(role)
        await db.flush()
        return role

    async def preview_update(
        self,
        db: AsyncSession,
        role_id: int,
        role_in: RoleUpdate,
        *,
        actor_user_id: int,
    ) -> RoleManagementPreview:
        return (
            await self._evaluate(
                db,
                action="update",
                actor_user_id=actor_user_id,
                role_id=role_id,
                role_in=role_in,
            )
        ).preview

    async def update(
        self,
        db: AsyncSession,
        role_id: int,
        role_in: RoleUpdate,
        *,
        actor_user_id: int,
        expected_snapshot: dict[str, Any] | None = None,
    ) -> Role:
        evaluation = await self._authorize_and_lock(
            db,
            action="update",
            actor_user_id=actor_user_id,
            role_id=role_id,
            role_in=role_in,
            expected_snapshot=expected_snapshot,
        )
        assert evaluation.role is not None
        values = role_in.model_dump(exclude_unset=True, exclude={"dept_ids"})
        for field, value in values.items():
            setattr(evaluation.role, field, value)
        if (
            "dept_ids" in role_in.model_fields_set
            or "data_scope" in role_in.model_fields_set
        ):
            evaluation.role.depts = list(evaluation.candidate_depts)
        return evaluation.role

    async def preview_update_menus(
        self,
        db: AsyncSession,
        role_id: int,
        menu_ids: list[int],
        *,
        actor_user_id: int,
    ) -> RoleManagementPreview:
        return (
            await self._evaluate(
                db,
                action="update_menus",
                actor_user_id=actor_user_id,
                role_id=role_id,
                menu_ids=menu_ids,
            )
        ).preview

    async def update_menus(
        self,
        db: AsyncSession,
        role_id: int,
        menu_ids: list[int],
        *,
        actor_user_id: int,
        expected_snapshot: dict[str, Any] | None = None,
    ) -> Role:
        evaluation = await self._authorize_and_lock(
            db,
            action="update_menus",
            actor_user_id=actor_user_id,
            role_id=role_id,
            menu_ids=menu_ids,
            expected_snapshot=expected_snapshot,
        )
        assert evaluation.role is not None
        evaluation.role.menus = list(evaluation.candidate_menus)
        return evaluation.role

    async def preview_update_agents(
        self,
        db: AsyncSession,
        role_id: int,
        agent_ids: list[int],
        *,
        actor_user_id: int,
    ) -> RoleManagementPreview:
        from app.modules.ai.service.role_agent import (  # noqa: PLC0415
            role_agent_service,
        )

        snapshot = await role_agent_service.preview_binding(
            db,
            role_id,
            agent_ids,
            actor_user_id=actor_user_id,
        )
        member_ids = tuple(
            int(value["userId"])
            for value in snapshot.get("members", [])
            if isinstance(value, dict) and "userId" in value
        )
        role = await self._load_role(db, role_id)
        return RoleManagementPreview(
            action="update_agents",
            role_id=role_id,
            member_user_ids=member_ids,
            snapshot=snapshot,
            target_role_name=role.role_name,
        )

    async def update_agents(
        self,
        db: AsyncSession,
        role_id: int,
        agent_ids: list[int],
        *,
        actor_user_id: int,
        expected_snapshot: dict[str, Any] | None = None,
    ) -> Role:
        from app.modules.ai.service.role_agent import (  # noqa: PLC0415
            role_agent_service,
        )

        await role_agent_service.put_binding(
            db,
            role_id,
            RoleAgentBindReq(agent_ids=[str(value) for value in agent_ids]),
            actor_user_id=actor_user_id,
            expected_snapshot=expected_snapshot,
        )
        return await self._load_role(db, role_id)

    async def authorize_role_projection(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        role_id: int,
    ) -> Role:
        """Reauthorize a managed Role result against current delegation facts."""
        authority = await grant_authority_service.build(db, actor_user_id)
        role = await self._load_role(db, role_id)
        members = await self._load_role_members(db, role_id)
        self._ensure_member_boundary(
            authority,
            actor_user_id=actor_user_id,
            role=role,
            members=members,
        )
        agent_ids = (await self._active_agent_ids_by_role(db, {int(role.role_id)})).get(
            int(role.role_id), set()
        )
        if not self._definition_dominated(
            authority,
            menus=role.menus,
            depts=role.depts,
            data_scope=role.data_scope,
            agent_ids=agent_ids,
        ):
            self._raise_authority_exceeded()
        current = await user_role_assignment_service.materialize_role_set_authorities(
            db,
            candidates=[
                (member, list(member.roles), list(member.depts)) for member in members
            ],
        )
        self._ensure_materialized_dominated(authority, current)
        return role

    async def summarize_roles(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        query: str | None = None,
        role_name: str | None = None,
        role_code: str | None = None,
        data_scope: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[RoleSummary], int, tuple[int, ...]]:
        authority = await grant_authority_service.build(db, actor_user_id)
        self._require_permission(authority, "system:role:list")
        filters = []
        if query:
            normalized = query.strip().casefold()
            filters.append(
                or_(
                    func.lower(Role.role_code).contains(normalized, autoescape=True),
                    func.lower(Role.role_name).contains(normalized, autoescape=True),
                )
            )
        if role_name:
            filters.append(
                func.lower(Role.role_name).contains(
                    role_name.strip().casefold(),
                    autoescape=True,
                )
            )
        if role_code:
            filters.append(
                func.lower(Role.role_code).contains(
                    role_code.strip().casefold(),
                    autoescape=True,
                )
            )
        if data_scope is not None:
            filters.append(Role.data_scope == data_scope)
        if status is not None:
            filters.append(Role.status == status)
        contributor_ids = tuple(
            int(value)
            for value in (
                await db.execute(
                    select(Role.role_id)
                    .where(*filters)
                    .order_by(Role.role_code, Role.role_id)
                )
            ).scalars()
        )
        total = len(contributor_ids)
        roles = list(
            (
                await db.execute(
                    select(Role)
                    .where(*filters)
                    .options(
                        selectinload(Role.menus),
                        selectinload(Role.depts),
                        selectinload(Role.users).selectinload(User.depts),
                        selectinload(Role.users)
                        .selectinload(User.roles)
                        .selectinload(Role.menus),
                        selectinload(Role.users)
                        .selectinload(User.roles)
                        .selectinload(Role.depts),
                    )
                    .order_by(Role.role_code, Role.role_id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .unique()
        )
        agents = await self._active_agent_ids_by_role(
            db,
            {int(role.role_id) for role in roles},
        )
        summaries: list[RoleSummary] = []
        for role in roles:
            reason: str | None = None
            member_ids = {int(user.user_id) for user in role.users}
            if not authority.super_admin and role.role_code in PROTECTED_ROLE_CODES:
                reason = "AI_ROLE_PROTECTED"
            elif not self._definition_dominated(
                authority,
                menus=role.menus,
                depts=role.depts,
                data_scope=role.data_scope,
                agent_ids=agents.get(int(role.role_id), set()),
            ):
                reason = "AI_ROLE_AUTHORITY_EXCEEDED"
            elif not authority.super_admin and actor_user_id in member_ids:
                reason = "AI_ROLE_SELF_MUTATION_FORBIDDEN"
            elif not authority.super_admin and any(
                member.user_name == ADMIN_USERNAME
                or any(
                    member_role.status == STATUS_ENABLED
                    and member_role.role_code in PROTECTED_ROLE_CODES
                    for member_role in member.roles
                )
                for member in role.users
            ):
                reason = "AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE"
            elif (
                not authority.super_admin
                and authority.accessible_user_scope is not None
                and not member_ids <= set(authority.accessible_user_scope)
            ):
                reason = "AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE"
            if reason is None and role.users:
                current = (
                    await user_role_assignment_service.materialize_role_set_authorities(
                        db,
                        candidates=[
                            (member, list(member.roles), list(member.depts))
                            for member in role.users
                        ],
                    )
                )
                try:
                    self._ensure_materialized_dominated(authority, current)
                except AuthorizationException:
                    reason = "AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE"
            summaries.append(
                RoleSummary(
                    role_id=int(role.role_id),
                    role_code=role.role_code,
                    role_name=role.role_name,
                    status=role.status,
                    data_scope=role.data_scope,
                    delegable=reason is None,
                    blocked_reason_code=reason,
                )
            )
        return summaries, total, contributor_ids


role_management_service = RoleManagementService()
