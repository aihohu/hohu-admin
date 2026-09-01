import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    DEPT_MAX_LEVEL,
    STATUS_ENABLED,
)
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    DuplicateException,
    InvalidParameterException,
    NotFoundException,
)
from app.core.tenant import TenantContext
from app.core.tenant_scope import tenant_filter, tenant_select
from app.db.base import role_depts, user_depts, user_roles
from app.modules.system.constants import PHASE3_DESTRUCTIVE_PERMISSIONS
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import (
    DeptCreate,
    DeptQuery,
    DeptUpdate,
)
from app.modules.system.service.authorization_lock import authorization_lock_service
from app.modules.system.service.grant_authority import (
    GrantAuthority,
    grant_authority_service,
)
from app.utils.pagination import build_filters, paginate


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DepartmentWritePreview:
    """Server-owned department write preview shared by page and AI callers."""

    action: Literal["create", "update", "move"]
    dept_id: int | None
    affected_user_ids: tuple[int, ...]
    snapshot: dict[str, Any]
    target_dept_name: str | None = None
    parent_dept_name: str | None = None


class DeptService:
    """部门业务逻辑服务"""

    async def get_list(
        self, db: AsyncSession, query: DeptQuery, *, tenant: TenantContext
    ):
        """获取分页列表"""
        field_mapping = {
            "dept_name": ("dept_name", "contains"),
            "status": ("status", "=="),
            "leader": ("leader", "contains"),
        }
        filters = build_filters(Dept, field_mapping, **query.model_dump())
        filters.insert(0, tenant_filter(Dept, tenant=tenant))
        return await paginate(
            db=db,
            model=Dept,
            query_params=query,
            filters=filters,
            order_by=Dept.order_num.asc(),
        )

    async def get_all(self, db: AsyncSession, *, tenant: TenantContext) -> list[Dept]:
        """获取全量列表（不分页），用于构建树"""
        stmt = tenant_select(Dept, tenant=tenant).order_by(Dept.order_num.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(
        self, db: AsyncSession, dept_id: int, *, tenant: TenantContext
    ) -> Dept:
        """根据 ID 获取部门"""
        dept = await db.scalar(
            tenant_select(Dept, tenant=tenant).where(Dept.dept_id == dept_id)
        )
        if not dept:
            raise NotFoundException("部门")
        return dept

    async def get_by_ids(
        self, db: AsyncSession, ids: list[int], *, tenant: TenantContext
    ) -> list[Dept]:
        """批量查询部门"""
        if not ids:
            return []
        result = await db.execute(
            tenant_select(Dept, tenant=tenant).where(Dept.dept_id.in_(ids))
        )
        return list(result.scalars().all())

    @staticmethod
    def _require_permissions(
        authority: GrantAuthority,
        permissions: set[str],
    ) -> None:
        if authority.allows_permission_codes(permissions):
            return
        raise AuthorizationException(
            "缺少部门管理权限",
            error_code="PERMISSION_DENIED",
        )

    @staticmethod
    def _ensure_dept_scope(authority: GrantAuthority, dept_ids: set[int]) -> None:
        if authority.super_admin or authority.accessible_dept_ids is None:
            return
        if dept_ids <= set(authority.accessible_dept_ids):
            return
        raise AuthorizationException(
            "部门不在当前数据权限范围内",
            error_code="AI_DATA_SCOPE_VIOLATION",
        )

    @staticmethod
    def _ancestor_ids(dept: Dept | None) -> set[int]:
        if dept is None or not dept.ancestors:
            return set()
        return {
            int(value) for value in dept.ancestors.split(",") if value and value != "0"
        }

    async def _load_department(
        self,
        db: AsyncSession,
        dept_id: int,
        *,
        tenant: TenantContext,
    ) -> Dept:
        dept = await db.scalar(
            tenant_select(Dept, tenant=tenant)
            .where(Dept.dept_id == dept_id)
            .execution_options(populate_existing=True)
        )
        if dept is None:
            raise NotFoundException("部门")
        return dept

    async def _load_users(
        self, db: AsyncSession, user_ids: set[int], *, tenant: TenantContext
    ) -> list[User]:
        if not user_ids:
            return []
        users = list(
            (
                await db.execute(
                    tenant_select(User, tenant=tenant)
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
            .scalars()
            .unique()
        )
        return users

    async def _affected_role_ids_for_status(
        self,
        db: AsyncSession,
        dept_id: int,
        *,
        tenant: TenantContext,
    ) -> set[int]:
        return {
            int(role_id)
            for role_id in (
                await db.execute(
                    select(role_depts.c.role_id)
                    .join(Role, Role.role_id == role_depts.c.role_id)
                    .where(
                        role_depts.c.tenant_id == tenant.tenant_id,
                        Role.tenant_id == tenant.tenant_id,
                        role_depts.c.dept_id == dept_id,
                        Role.status == STATUS_ENABLED,
                        Role.data_scope == DATA_SCOPE_CUSTOM,
                    )
                )
            ).scalars()
        }

    async def _affected_role_ids_for_move(
        self,
        db: AsyncSession,
        anchor_ids: set[int],
        *,
        tenant: TenantContext,
    ) -> set[int]:
        if not anchor_ids:
            return set()
        return {
            int(role_id)
            for role_id in (
                await db.execute(
                    select(Role.role_id)
                    .join(user_roles, user_roles.c.role_id == Role.role_id)
                    .join(
                        user_depts,
                        user_depts.c.user_id == user_roles.c.user_id,
                    )
                    .where(
                        Role.tenant_id == tenant.tenant_id,
                        user_roles.c.tenant_id == tenant.tenant_id,
                        user_depts.c.tenant_id == tenant.tenant_id,
                        Role.status == STATUS_ENABLED,
                        Role.data_scope == DATA_SCOPE_DEPT_AND_SUB,
                        user_depts.c.dept_id.in_(anchor_ids),
                    )
                    .distinct()
                )
            ).scalars()
        }

    async def _member_ids_for_roles(
        self,
        db: AsyncSession,
        role_ids: set[int],
        *,
        tenant: TenantContext,
    ) -> set[int]:
        if not role_ids:
            return set()
        return {
            int(user_id)
            for user_id in (
                await db.execute(
                    select(user_roles.c.user_id).where(
                        user_roles.c.tenant_id == tenant.tenant_id,
                        user_roles.c.role_id.in_(role_ids),
                    )
                )
            ).scalars()
        }

    async def _resolve_leader_reference(
        self,
        db: AsyncSession,
        *,
        authority: GrantAuthority,
        leader: str | None,
    ) -> dict[str, str] | None:
        """Resolve one leader label uniquely inside the actor's current user scope."""
        if leader is None or not leader.strip():
            return None
        normalized = leader.strip().casefold()
        filters = [
            User.tenant_id == authority.tenant_id,
            or_(
                func.lower(User.user_name) == normalized,
                func.lower(User.nickname) == normalized,
            ),
        ]
        if authority.accessible_user_scope is not None:
            filters.append(User.user_id.in_(authority.accessible_user_scope))
        matches = list(
            (
                await db.execute(
                    select(User).where(*filters).order_by(User.user_id).limit(2)
                )
            ).scalars()
        )
        if not matches:
            raise NotFoundException(
                "部门负责人",
                error_code="AI_DEPT_LEADER_NOT_FOUND",
            )
        if len(matches) != 1:
            raise BusinessRuleException(
                "部门负责人匹配不唯一",
                error_code="AI_LOOKUP_AMBIGUOUS",
            )
        user = matches[0]
        return {
            "userId": str(user.user_id),
            "display": f"{user.user_name} ({user.nickname or user.user_name})",
        }

    async def _affected_role_facts_for_status(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        authority: GrantAuthority,
        role_ids: set[int],
        status_override: tuple[int, str],
        error_code: str,
    ) -> tuple[list[dict[str, Any]], set[int]]:
        """Authorize and freeze every affected Role, including empty roles."""
        if not role_ids:
            return [], set()
        roles = list(
            (
                await db.execute(
                    select(Role)
                    .where(
                        Role.tenant_id == tenant.tenant_id,
                        Role.role_id.in_(role_ids),
                    )
                    .options(selectinload(Role.menus), selectinload(Role.depts))
                    .order_by(Role.role_id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .unique()
        )
        from app.modules.ai.service.agent_authorization_service import (  # noqa: PLC0415
            agent_authorization_service,
        )

        agents_by_role = (
            await agent_authorization_service.grantable_agent_ids_by_role_ids(
                db,
                role_ids,
                tenant=tenant,
            )
        )
        changed_dept_id, candidate_status = status_override
        facts: list[dict[str, Any]] = []
        dependency_dept_ids: set[int] = set()
        for role in roles:
            all_dept_ids = {int(dept.dept_id) for dept in role.depts}
            before_dept_ids = {
                int(dept.dept_id)
                for dept in role.depts
                if dept.status == STATUS_ENABLED
            }
            after_dept_ids = set(before_dept_ids)
            if changed_dept_id in all_dept_ids:
                if candidate_status == STATUS_ENABLED:
                    after_dept_ids.add(changed_dept_id)
                else:
                    after_dept_ids.discard(changed_dept_id)
            dependency_dept_ids.update(all_dept_ids)
            permission_codes = {
                menu.permission for menu in role.menus if menu.permission
            }
            menu_ids = {int(menu.menu_id) for menu in role.menus}
            agent_ids = agents_by_role.get(int(role.role_id), set())
            if not authority.super_admin and not (
                authority.allows_permission_codes(permission_codes)
                and authority.allows_menu_ids(menu_ids)
                and authority.allows_agent_ids(agent_ids)
                and authority.allows_scope_kind(role.data_scope, before_dept_ids)
                and authority.allows_scope_kind(role.data_scope, after_dept_ids)
            ):
                raise AuthorizationException(
                    "部门状态变化影响了范围外角色",
                    error_code=error_code,
                )
            facts.append(
                {
                    "roleId": str(role.role_id),
                    "status": role.status,
                    "dataScope": role.data_scope,
                    "menuIds": sorted(menu_ids),
                    "permissionCodes": sorted(permission_codes),
                    "agentIds": sorted(agent_ids),
                    "beforeDeptIds": sorted(before_dept_ids),
                    "afterDeptIds": sorted(after_dept_ids),
                }
            )
        return facts, dependency_dept_ids

    @staticmethod
    def _descendant_ids(
        root_ids: set[int],
        parent_by_id: dict[int, int | None],
    ) -> set[int]:
        result = set(root_ids)
        changed = True
        while changed:
            changed = False
            for dept_id, parent_id in parent_by_id.items():
                if dept_id not in result and parent_id in result:
                    result.add(dept_id)
                    changed = True
        return result

    @classmethod
    def _materialized_scope(
        cls,
        user: User,
        *,
        parent_by_id: dict[int, int | None],
        status_by_id: dict[int, str],
        users_by_dept: dict[int, set[int]],
    ) -> tuple[set[int] | None, set[int] | None]:
        roles = [role for role in user.roles if role.status == STATUS_ENABLED]
        if any(role.data_scope == DATA_SCOPE_ALL for role in roles):
            return None, None

        own_dept_ids = {int(dept.dept_id) for dept in user.depts}
        dept_ids: set[int] = set()
        include_self = False
        for role in roles:
            if role.data_scope == DATA_SCOPE_SELF:
                include_self = True
            elif role.data_scope == DATA_SCOPE_DEPT:
                dept_ids.update(own_dept_ids)
                include_self = include_self or not own_dept_ids
            elif role.data_scope == DATA_SCOPE_DEPT_AND_SUB:
                subtree = cls._descendant_ids(own_dept_ids, parent_by_id)
                dept_ids.update(subtree)
                include_self = include_self or not subtree
            elif role.data_scope == DATA_SCOPE_CUSTOM:
                custom_ids = {
                    int(dept.dept_id)
                    for dept in role.depts
                    if status_by_id.get(int(dept.dept_id)) == STATUS_ENABLED
                }
                dept_ids.update(custom_ids)
                include_self = include_self or not custom_ids
        if not roles:
            include_self = True

        user_ids = {
            user_id
            for dept_id in dept_ids
            for user_id in users_by_dept.get(dept_id, set())
        }
        if include_self or bool(own_dept_ids & dept_ids):
            user_ids.add(int(user.user_id))
        return dept_ids, user_ids

    async def _impact_snapshot(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        authority: GrantAuthority,
        affected_users: list[User],
        status_override: tuple[int, str] | None = None,
        parent_override: tuple[int, int | None] | None = None,
        error_code: str,
    ) -> tuple[dict[str, Any], set[int]]:
        departments = list(
            (
                await db.execute(
                    tenant_select(Dept, tenant=tenant).order_by(Dept.dept_id)
                )
            ).scalars()
        )
        parent_before = {
            int(dept.dept_id): (
                int(dept.parent_id) if dept.parent_id is not None else None
            )
            for dept in departments
        }
        status_before = {int(dept.dept_id): str(dept.status) for dept in departments}
        parent_after = dict(parent_before)
        status_after = dict(status_before)
        if parent_override is not None:
            parent_after[parent_override[0]] = parent_override[1]
        if status_override is not None:
            status_after[status_override[0]] = status_override[1]

        memberships = (
            await db.execute(
                select(user_depts.c.dept_id, user_depts.c.user_id).where(
                    user_depts.c.tenant_id == tenant.tenant_id
                )
            )
        ).all()
        users_by_dept: dict[int, set[int]] = {}
        for dept_id, user_id in memberships:
            users_by_dept.setdefault(int(dept_id), set()).add(int(user_id))

        impact: dict[str, Any] = {}
        dependency_dept_ids: set[int] = set()
        for user in affected_users:
            before_depts, before_users = self._materialized_scope(
                user,
                parent_by_id=parent_before,
                status_by_id=status_before,
                users_by_dept=users_by_dept,
            )
            after_depts, after_users = self._materialized_scope(
                user,
                parent_by_id=parent_after,
                status_by_id=status_after,
                users_by_dept=users_by_dept,
            )
            if before_depts is not None:
                dependency_dept_ids.update(before_depts)
            if after_depts is not None:
                dependency_dept_ids.update(after_depts)
            if not authority.super_admin:
                actor_user_scope = authority.accessible_user_scope
                if (
                    actor_user_scope is not None
                    and int(user.user_id) not in actor_user_scope
                ):
                    raise AuthorizationException(
                        "部门变更影响了范围外账号",
                        error_code=error_code,
                    )
                for dept_ids, user_ids in (
                    (before_depts, before_users),
                    (after_depts, after_users),
                ):
                    if dept_ids is None or user_ids is None:
                        allowed = (
                            authority.accessible_dept_ids is None
                            and authority.accessible_user_scope is None
                        )
                    else:
                        allowed = authority.allows_materialized_scope(
                            dept_ids=dept_ids,
                            user_ids=user_ids,
                        )
                    if not allowed:
                        raise AuthorizationException(
                            "部门变更超出当前操作者授权上界",
                            error_code=error_code,
                        )
            impact[str(user.user_id)] = {
                "roleIds": sorted(int(role.role_id) for role in user.roles),
                "deptIds": sorted(int(dept.dept_id) for dept in user.depts),
                "before": {
                    "departments": None
                    if before_depts is None
                    else sorted(before_depts),
                    "users": None if before_users is None else sorted(before_users),
                },
                "after": {
                    "departments": None if after_depts is None else sorted(after_depts),
                    "users": None if after_users is None else sorted(after_users),
                },
            }
        return impact, dependency_dept_ids

    async def _build_preview(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        action: Literal["create", "update", "move"],
        actor_user_id: int,
        dept_in: DeptCreate | DeptUpdate | None = None,
        dept_id: int | None = None,
        new_parent_id: int | None = None,
    ) -> tuple[DepartmentWritePreview, set[int], set[int], set[int]]:
        authority = await grant_authority_service.build(
            db, actor_user_id, tenant=tenant
        )
        required = {
            "create": {"system:dept:add", "system:dept:list"},
            "update": {"system:dept:edit", "system:dept:list"},
            "move": {"system:dept:move", "system:dept:list"},
        }[action]
        self._require_permissions(authority, required)

        affected_role_ids: set[int] = set()
        affected_user_ids: set[int] = set()
        dependency_dept_ids: set[int] = set()
        candidate: dict[str, Any]
        target: Dept | None = None
        parent: Dept | None = None
        impact: dict[str, Any] = {}
        affected_role_facts: list[dict[str, Any]] = []
        leader_fact: dict[str, str] | None = None

        if action == "create":
            assert isinstance(dept_in, DeptCreate)
            leader_fact = await self._resolve_leader_reference(
                db,
                authority=authority,
                leader=dept_in.leader,
            )
            parent_id = int(dept_in.parent_id) if dept_in.parent_id else None
            if parent_id is None and not authority.super_admin:
                raise AuthorizationException(
                    "仅超级管理员可以创建顶级部门",
                    error_code="AI_DEPT_ROOT_CREATE_FORBIDDEN",
                )
            if parent_id is not None:
                parent = await self._load_department(db, parent_id, tenant=tenant)
                self._ensure_dept_scope(authority, {parent_id})
                if parent.status != STATUS_ENABLED:
                    raise BusinessRuleException(
                        "父部门已禁用",
                        error_code="AI_DEPT_PARENT_DISABLED",
                    )
                if self._get_dept_level(parent.ancestors) + 1 > DEPT_MAX_LEVEL:
                    raise BusinessRuleException(f"部门层级不能超过{DEPT_MAX_LEVEL}层")
                dependency_dept_ids.update({parent_id, *self._ancestor_ids(parent)})
            await self._check_duplicate_name(
                db, parent_id, dept_in.dept_name, tenant=tenant
            )
            candidate = dept_in.model_dump(mode="json")
        else:
            assert dept_id is not None
            target = await self._load_department(db, dept_id, tenant=tenant)
            self._ensure_dept_scope(authority, {dept_id})
            dependency_dept_ids.update({dept_id, *self._ancestor_ids(target)})
            if action == "update":
                assert isinstance(dept_in, DeptUpdate)
                update_data = dept_in.model_dump(exclude_unset=True)
                if "leader" in update_data:
                    leader_fact = await self._resolve_leader_reference(
                        db,
                        authority=authority,
                        leader=update_data["leader"],
                    )
                new_name = update_data.get("dept_name")
                if new_name and new_name != target.dept_name:
                    await self._check_duplicate_name(
                        db,
                        target.parent_id,
                        str(new_name),
                        exclude_id=dept_id,
                        tenant=tenant,
                    )
                candidate = update_data
                if "status" in update_data and update_data["status"] != target.status:
                    affected_role_ids = await self._affected_role_ids_for_status(
                        db, dept_id, tenant=tenant
                    )
                    (
                        affected_role_facts,
                        affected_role_depts,
                    ) = await self._affected_role_facts_for_status(
                        db,
                        tenant=tenant,
                        authority=authority,
                        role_ids=affected_role_ids,
                        status_override=(dept_id, str(update_data["status"])),
                        error_code="AI_DEPT_STATUS_AUTHZ_IMPACT_OUT_OF_SCOPE",
                    )
                    dependency_dept_ids.update(affected_role_depts)
                    affected_user_ids = await self._member_ids_for_roles(
                        db, affected_role_ids, tenant=tenant
                    )
                    affected_users = await self._load_users(
                        db, affected_user_ids, tenant=tenant
                    )
                    impact, impact_depts = await self._impact_snapshot(
                        db,
                        tenant=tenant,
                        authority=authority,
                        affected_users=affected_users,
                        status_override=(dept_id, str(update_data["status"])),
                        error_code="AI_DEPT_STATUS_AUTHZ_IMPACT_OUT_OF_SCOPE",
                    )
                    dependency_dept_ids.update(impact_depts)
            else:
                if target.parent_id == new_parent_id:
                    raise BusinessRuleException(
                        "部门已经位于目标父部门下",
                        error_code="AI_DEPT_MOVE_UNCHANGED",
                    )
                actor_dept_ids = authority.accessible_dept_ids
                scope_root = target.parent_id is None or (
                    actor_dept_ids is not None
                    and int(target.parent_id) not in actor_dept_ids
                )
                if scope_root and not authority.super_admin:
                    raise AuthorizationException(
                        "仅超级管理员可以移动范围根节点",
                        error_code="AI_DEPT_SCOPE_ROOT_MOVE_FORBIDDEN",
                    )
                if new_parent_id is None and not authority.super_admin:
                    raise AuthorizationException(
                        "仅超级管理员可以移动到根节点",
                        error_code="AI_DEPT_ROOT_MOVE_FORBIDDEN",
                    )
                old_parent = (
                    await self._load_department(
                        db, int(target.parent_id), tenant=tenant
                    )
                    if target.parent_id is not None
                    else None
                )
                if new_parent_id is not None:
                    parent = await self._load_department(
                        db, new_parent_id, tenant=tenant
                    )
                    if parent.status != STATUS_ENABLED:
                        raise BusinessRuleException(
                            "目标父部门已禁用",
                            error_code="AI_DEPT_PARENT_DISABLED",
                        )
                    if new_parent_id == dept_id or dept_id in self._ancestor_ids(
                        parent
                    ):
                        raise BusinessRuleException(
                            "不能把部门移动到自己或后代下",
                            error_code="AI_DEPT_MOVE_CYCLE",
                        )
                    child_depth = await self._get_max_child_depth(
                        db, dept_id, tenant=tenant
                    )
                    if (
                        self._get_dept_level(parent.ancestors) + 1 + child_depth
                        > DEPT_MAX_LEVEL
                    ):
                        raise BusinessRuleException(
                            f"部门层级不能超过{DEPT_MAX_LEVEL}层"
                        )
                direct_ids = {
                    dept_id,
                    *(
                        {int(target.parent_id)}
                        if target.parent_id is not None
                        else set()
                    ),
                    *({new_parent_id} if new_parent_id is not None else set()),
                }
                subtree_ids = {
                    int(value)
                    for value in (
                        await db.execute(
                            select(Dept.dept_id).where(
                                Dept.tenant_id == tenant.tenant_id,
                                (Dept.dept_id == dept_id)
                                | (Dept.ancestors == f"{target.ancestors},{dept_id}")
                                | Dept.ancestors.like(
                                    f"{target.ancestors},{dept_id},%"
                                ),
                            )
                        )
                    ).scalars()
                }
                self._ensure_dept_scope(authority, direct_ids | subtree_ids)
                dependency_dept_ids.update(direct_ids | subtree_ids)
                if old_parent is not None:
                    dependency_dept_ids.update(self._ancestor_ids(old_parent))
                if parent is not None:
                    dependency_dept_ids.update(self._ancestor_ids(parent))
                anchors = {
                    *(self._ancestor_ids(old_parent) if old_parent else set()),
                    *(self._ancestor_ids(parent) if parent else set()),
                    *({int(old_parent.dept_id)} if old_parent else set()),
                    *({int(parent.dept_id)} if parent else set()),
                }
                affected_role_ids = await self._affected_role_ids_for_move(
                    db, anchors, tenant=tenant
                )
                affected_user_ids = await self._member_ids_for_roles(
                    db, affected_role_ids, tenant=tenant
                )
                affected_users = await self._load_users(
                    db, affected_user_ids, tenant=tenant
                )
                impact, impact_depts = await self._impact_snapshot(
                    db,
                    tenant=tenant,
                    authority=authority,
                    affected_users=affected_users,
                    parent_override=(dept_id, new_parent_id),
                    error_code="AI_DEPT_MOVE_AUTHZ_IMPACT_OUT_OF_SCOPE",
                )
                dependency_dept_ids.update(impact_depts)
                candidate = {"newParentId": new_parent_id}

        affected_users = await self._load_users(db, affected_user_ids, tenant=tenant)
        role_ids = set(authority.enabled_role_ids)
        role_ids.update(affected_role_ids)
        role_ids.update(
            int(role.role_id) for user in affected_users for role in user.roles
        )
        user_ids = {actor_user_id, *affected_user_ids}
        if leader_fact is not None:
            user_ids.add(int(leader_fact["userId"]))
        department_facts = list(
            (
                await db.execute(
                    select(
                        Dept.dept_id,
                        Dept.parent_id,
                        Dept.ancestors,
                        Dept.status,
                    )
                    .where(Dept.tenant_id == tenant.tenant_id)
                    .order_by(Dept.dept_id)
                )
            ).all()
        )
        snapshot_payload = {
            "action": action,
            "actorAuthorityVersion": authority.version_summary,
            "targetDeptId": str(dept_id) if dept_id is not None else None,
            "candidate": candidate,
            "roleIds": sorted(role_ids),
            "deptIds": sorted(dependency_dept_ids),
            "userIds": sorted(user_ids),
            "departmentFacts": [
                [
                    str(row.dept_id),
                    str(row.parent_id) if row.parent_id is not None else None,
                    row.ancestors,
                    row.status,
                ]
                for row in department_facts
            ],
            "impact": impact,
            "affectedRoles": affected_role_facts,
            "leader": leader_fact,
        }
        snapshot = {
            "version": "phase3-dept-write/v1",
            "digest": _canonical_hash(snapshot_payload),
            "facts": snapshot_payload,
        }
        return (
            DepartmentWritePreview(
                action=action,
                dept_id=dept_id,
                affected_user_ids=tuple(sorted(affected_user_ids)),
                snapshot=snapshot,
                target_dept_name=target.dept_name if target is not None else None,
                parent_dept_name=parent.dept_name if parent is not None else None,
            ),
            role_ids,
            dependency_dept_ids,
            user_ids,
        )

    async def _authorize_and_lock(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        action: Literal["create", "update", "move"],
        actor_user_id: int,
        dept_in: DeptCreate | DeptUpdate | None = None,
        dept_id: int | None = None,
        new_parent_id: int | None = None,
        expected_snapshot: dict[str, Any] | None,
    ) -> DepartmentWritePreview:
        initial, role_ids, dept_ids, user_ids = await self._build_preview(
            db,
            tenant=tenant,
            action=action,
            actor_user_id=actor_user_id,
            dept_in=dept_in,
            dept_id=dept_id,
            new_parent_id=new_parent_id,
        )
        await authorization_lock_service.lock_targets(
            db,
            role_ids=role_ids,
            dept_ids=dept_ids,
            user_ids=user_ids,
            tenant=tenant,
        )
        try:
            (
                locked,
                locked_roles,
                locked_depts,
                locked_users,
            ) = await self._build_preview(
                db,
                tenant=tenant,
                action=action,
                actor_user_id=actor_user_id,
                dept_in=dept_in,
                dept_id=dept_id,
                new_parent_id=new_parent_id,
            )
        except (AuthorizationException, BusinessRuleException) as exc:
            raise BusinessRuleException(
                "部门授权事实在锁后发生变化",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            ) from exc
        if (
            initial.snapshot != locked.snapshot
            or role_ids != locked_roles
            or dept_ids != locked_depts
            or user_ids != locked_users
        ):
            raise BusinessRuleException(
                "部门授权事实已变化",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        if expected_snapshot is not None and locked.snapshot != expected_snapshot:
            raise BusinessRuleException(
                "部门审批快照已变化",
                error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE",
            )
        return locked

    async def preview_create(
        self,
        db: AsyncSession,
        dept_in: DeptCreate,
        *,
        actor_user_id: int,
        tenant: TenantContext,
    ) -> DepartmentWritePreview:
        preview, *_ = await self._build_preview(
            db,
            tenant=tenant,
            action="create",
            actor_user_id=actor_user_id,
            dept_in=dept_in,
        )
        return preview

    async def create(
        self,
        db: AsyncSession,
        dept_in: DeptCreate,
        *,
        actor_user_id: int,
        tenant: TenantContext,
        expected_snapshot: dict[str, Any] | None = None,
    ) -> Dept:
        """Create a scoped department after global authorization revalidation."""
        await self._authorize_and_lock(
            db,
            tenant=tenant,
            action="create",
            actor_user_id=actor_user_id,
            dept_in=dept_in,
            expected_snapshot=expected_snapshot,
        )
        parent_id = int(dept_in.parent_id) if dept_in.parent_id else None
        parent = (
            await self._load_department(db, parent_id, tenant=tenant)
            if parent_id is not None
            else None
        )
        new_dept = Dept(
            tenant_id=tenant.tenant_id,
            parent_id=parent_id,
            ancestors=(f"{parent.ancestors},{parent.dept_id}" if parent else "0"),
            dept_name=dept_in.dept_name,
            order_num=dept_in.order_num,
            leader=dept_in.leader,
            phone=dept_in.phone,
            email=dept_in.email or None,
            status=dept_in.status,
        )
        db.add(new_dept)
        await db.flush()
        return new_dept

    async def preview_update(
        self,
        db: AsyncSession,
        dept_id: int,
        dept_in: DeptUpdate,
        *,
        actor_user_id: int,
        tenant: TenantContext,
    ) -> DepartmentWritePreview:
        preview, *_ = await self._build_preview(
            db,
            tenant=tenant,
            action="update",
            actor_user_id=actor_user_id,
            dept_id=dept_id,
            dept_in=dept_in,
        )
        return preview

    async def update(
        self,
        db: AsyncSession,
        dept_id: int,
        dept_in: DeptUpdate,
        *,
        actor_user_id: int,
        tenant: TenantContext,
        expected_snapshot: dict[str, Any] | None = None,
    ) -> Dept:
        """Update scoped non-structural fields after authorization revalidation."""
        await self._authorize_and_lock(
            db,
            tenant=tenant,
            action="update",
            actor_user_id=actor_user_id,
            dept_id=dept_id,
            dept_in=dept_in,
            expected_snapshot=expected_snapshot,
        )
        dept = await self._load_department(db, dept_id, tenant=tenant)
        for field, value in dept_in.model_dump(exclude_unset=True).items():
            setattr(dept, field, value)
        return dept

    async def preview_move(
        self,
        db: AsyncSession,
        *,
        dept_id: int,
        new_parent_id: int | None,
        actor_user_id: int,
        tenant: TenantContext,
    ) -> DepartmentWritePreview:
        preview, *_ = await self._build_preview(
            db,
            tenant=tenant,
            action="move",
            actor_user_id=actor_user_id,
            dept_id=dept_id,
            new_parent_id=new_parent_id,
        )
        return preview

    async def move(
        self,
        db: AsyncSession,
        *,
        dept_id: int,
        new_parent_id: int | None,
        actor_user_id: int,
        tenant: TenantContext,
        expected_snapshot: dict[str, Any] | None = None,
    ) -> Dept:
        """Move one scoped subtree after global authorization revalidation."""
        await self._authorize_and_lock(
            db,
            tenant=tenant,
            action="move",
            actor_user_id=actor_user_id,
            dept_id=dept_id,
            new_parent_id=new_parent_id,
            expected_snapshot=expected_snapshot,
        )
        dept = await self._load_department(db, dept_id, tenant=tenant)
        old_prefix = str(dept.ancestors)
        parent = (
            await self._load_department(db, new_parent_id, tenant=tenant)
            if new_parent_id is not None
            else None
        )
        new_prefix = f"{parent.ancestors},{parent.dept_id}" if parent else "0"
        await self._update_descendants_ancestors(
            db,
            dept_id,
            old_prefix,
            new_prefix,
            tenant=tenant,
        )
        dept.parent_id = new_parent_id
        dept.ancestors = new_prefix
        return dept

    async def delete(
        self,
        db: AsyncSession,
        dept_id: int,
        *,
        actor_user_id: int,
        tenant: TenantContext,
    ) -> None:
        """Delete one unreferenced department through the destructive policy."""
        await self._delete_departments(
            db,
            [dept_id],
            actor_user_id=actor_user_id,
            required_permission="system:dept:delete",
            tenant=tenant,
        )

    async def _delete_facts(
        self,
        db: AsyncSession,
        dept_ids: tuple[int, ...],
        *,
        tenant: TenantContext,
    ) -> dict[str, tuple[int, ...]]:
        existing = tuple(
            int(value)
            for value in (
                await db.execute(
                    select(Dept.dept_id)
                    .where(
                        Dept.tenant_id == tenant.tenant_id,
                        Dept.dept_id.in_(dept_ids),
                    )
                    .order_by(Dept.dept_id)
                )
            ).scalars()
        )
        if existing != dept_ids:
            raise NotFoundException("部门")
        children = tuple(
            int(value)
            for value in (
                await db.execute(
                    select(Dept.dept_id)
                    .where(
                        Dept.tenant_id == tenant.tenant_id,
                        Dept.parent_id.in_(dept_ids),
                    )
                    .order_by(Dept.dept_id)
                )
            ).scalars()
        )
        direct_users = tuple(
            sorted(
                int(value)
                for value in (
                    await db.execute(
                        select(user_depts.c.user_id).where(
                            user_depts.c.tenant_id == tenant.tenant_id,
                            user_depts.c.dept_id.in_(dept_ids),
                        )
                    )
                ).scalars()
            )
        )
        referenced_roles = tuple(
            sorted(
                int(value)
                for value in (
                    await db.execute(
                        select(role_depts.c.role_id).where(
                            role_depts.c.tenant_id == tenant.tenant_id,
                            role_depts.c.dept_id.in_(dept_ids),
                        )
                    )
                ).scalars()
            )
        )
        role_members = tuple(
            sorted(
                int(value)
                for value in (
                    await db.execute(
                        select(user_roles.c.user_id).where(
                            user_roles.c.tenant_id == tenant.tenant_id,
                            user_roles.c.role_id.in_(referenced_roles),
                        )
                    )
                ).scalars()
            )
            if referenced_roles
            else ()
        )
        return {
            "targets": existing,
            "children": children,
            "directUsers": direct_users,
            "referencedRoles": referenced_roles,
            "roleMembers": role_members,
        }

    async def batch_delete(
        self,
        db: AsyncSession,
        ids: list[int],
        *,
        actor_user_id: int,
        tenant: TenantContext,
    ) -> int:
        """Atomically delete an unreferenced department set as super admin."""
        return await self._delete_departments(
            db,
            ids,
            actor_user_id=actor_user_id,
            required_permission="system:dept:batch-delete",
            tenant=tenant,
        )

    async def _delete_departments(
        self,
        db: AsyncSession,
        ids: list[int],
        *,
        actor_user_id: int,
        required_permission: str,
        tenant: TenantContext,
    ) -> int:
        """Enforce the exact destructive permission before one atomic delete."""
        if required_permission not in PHASE3_DESTRUCTIVE_PERMISSIONS:
            raise RuntimeError("unsupported department destructive permission")
        if not ids:
            raise InvalidParameterException("请选择要删除的部门")
        normalized = tuple(sorted({int(value) for value in ids}))
        authority = await grant_authority_service.build(
            db, actor_user_id, tenant=tenant
        )
        if not authority.super_admin:
            raise AuthorizationException(
                "仅超级管理员可以删除部门",
                error_code="SUPER_ADMIN_REQUIRED",
            )
        if required_permission not in authority.permission_codes:
            raise AuthorizationException(
                "缺少部门删除权限",
                error_code="MISSING_PERMISSION",
            )
        initial = await self._delete_facts(db, normalized, tenant=tenant)
        role_ids = {
            *authority.enabled_role_ids,
            *initial["referencedRoles"],
        }
        dept_ids = {*normalized, *initial["children"]}
        user_ids = {
            actor_user_id,
            *initial["directUsers"],
            *initial["roleMembers"],
        }
        await authorization_lock_service.lock_targets(
            db,
            role_ids=role_ids,
            dept_ids=dept_ids,
            user_ids=user_ids,
            tenant=tenant,
        )
        locked = await self._delete_facts(db, normalized, tenant=tenant)
        if locked != initial:
            raise BusinessRuleException(
                "部门删除引用事实已变化",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        if locked["children"] or locked["directUsers"] or locked["referencedRoles"]:
            raise BusinessRuleException(
                "部门仍被组织、用户或角色授权引用",
                error_code="DEPT_DELETE_REFERENCED",
            )
        result = await db.execute(
            delete(Dept).where(
                Dept.tenant_id == tenant.tenant_id,
                Dept.dept_id.in_(normalized),
            )
        )
        return int(result.rowcount or 0)

    def _get_dept_level(self, ancestors: str | None) -> int:
        """根据 ancestors 计算当前层级"""
        if not ancestors:
            return 1
        return len(ancestors.split(","))

    async def _get_max_child_depth(
        self, db: AsyncSession, dept_id: int, *, tenant: TenantContext
    ) -> int:
        """获取子树最大深度（相对于当前节点）"""
        dept = await db.scalar(
            tenant_select(Dept, tenant=tenant).where(Dept.dept_id == dept_id)
        )
        if not dept:
            return 0

        # 查询所有后代
        ancestor_prefix = f"{dept.ancestors},{dept_id}"
        stmt = select(Dept).where(
            Dept.tenant_id == tenant.tenant_id,
            or_(
                Dept.ancestors == ancestor_prefix,
                Dept.ancestors.like(f"{ancestor_prefix},%"),
            ),
        )
        result = await db.execute(stmt)
        descendants = result.scalars().all()

        if not descendants:
            return 0

        # 计算最大深度
        max_depth = 0
        base_level = len(ancestor_prefix.split(","))
        for desc in descendants:
            level = len(desc.ancestors.split(","))
            depth = level - base_level
            if depth > max_depth:
                max_depth = depth

        return max_depth

    async def _update_descendants_ancestors(
        self,
        db: AsyncSession,
        dept_id: int,
        old_prefix: str,
        new_prefix: str,
        *,
        tenant: TenantContext,
    ) -> None:
        """移动部门时更新后代 ancestors"""
        ancestor_pattern = f"{old_prefix},{dept_id}"
        stmt = select(Dept).where(
            Dept.tenant_id == tenant.tenant_id,
            or_(
                Dept.ancestors == ancestor_pattern,
                Dept.ancestors.like(f"{ancestor_pattern},%"),
            ),
        )
        result = await db.execute(stmt)
        descendants = result.scalars().all()

        for desc in descendants:
            desc.ancestors = desc.ancestors.replace(old_prefix, new_prefix, 1)

    async def _check_duplicate_name(
        self,
        db: AsyncSession,
        parent_id: int | None,
        dept_name: str,
        exclude_id: int | None = None,
        *,
        tenant: TenantContext,
    ) -> None:
        """校验同级名称唯一性"""
        stmt = select(Dept).where(
            Dept.tenant_id == tenant.tenant_id,
            Dept.dept_name == dept_name,
        )
        if parent_id is not None:
            stmt = stmt.where(Dept.parent_id == parent_id)
        else:
            stmt = stmt.where(Dept.parent_id.is_(None))

        if exclude_id:
            stmt = stmt.where(Dept.dept_id != exclude_id)

        result = await db.execute(stmt)
        if result.scalars().first():
            raise DuplicateException("同级部门名称", dept_name)


# 创建单例
dept_service = DeptService()
