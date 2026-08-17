"""Shared data-scope resolution and SQLAlchemy filters.

Every enabled role contributes its own materialized scope. Runtime consumers use
the union so incomparable ``DEPT``/``CUSTOM`` grants are never collapsed by an
integer priority. ``get_best_scope`` remains available only for upgrade audits
that must reproduce the legacy behavior.

用法示例（模型有 dept_id 字段）:
    filters = build_filters(...)
    scope_filters = await get_data_scope_filters(db, current_user, SomeModel)
    filters.extend(scope_filters)
    page_data = await paginate(db=db, model=SomeModel, filters=filters, ...)

用法示例（User 模型，多对多部门关系）:
    scope_filters = await get_user_data_scope_filters(db, current_user)
    filters.extend(scope_filters)
    page_data = await paginate(db=db, model=User, filters=filters, ...)
"""

from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
)
from app.core.rbac import is_super_admin
from app.db.base import role_depts, user_depts
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

DATA_SCOPE_UNION_RESOLVER_VERSION = "data-scope-union/v1"

KNOWN_DATA_SCOPES = frozenset(
    {
        DATA_SCOPE_ALL,
        DATA_SCOPE_CUSTOM,
        DATA_SCOPE_DEPT,
        DATA_SCOPE_DEPT_AND_SUB,
        DATA_SCOPE_SELF,
    }
)


@dataclass(frozen=True)
class DataScopeResolution:
    """One canonical materialized authorization view for a principal."""

    scope_kinds: frozenset[str]
    accessible_dept_ids: frozenset[int] | None
    accessible_user_scope: Select[tuple[int]] | None
    include_self: bool
    unbounded: bool


def _enabled_roles(user: User) -> list[Role]:
    return [role for role in (user.roles or []) if role.status == STATUS_ENABLED]


async def _custom_dept_ids_by_role(
    db: AsyncSession,
    role_ids: set[int],
) -> dict[int, set[int]]:
    result = {role_id: set() for role_id in role_ids}
    if not role_ids:
        return result
    rows = (
        await db.execute(
            select(role_depts.c.role_id, role_depts.c.dept_id)
            .join(Dept, Dept.dept_id == role_depts.c.dept_id)
            .where(
                role_depts.c.role_id.in_(role_ids),
                Dept.status == STATUS_ENABLED,
            )
        )
    ).all()
    for role_id, dept_id in rows:
        result[int(role_id)].add(int(dept_id))
    return result


def _build_user_scope(
    *,
    user_id: int,
    dept_ids: frozenset[int],
    include_self: bool,
) -> Select[tuple[int]]:
    statements: list[Select[tuple[int]]] = []
    if include_self:
        statements.append(
            select(User.user_id.label("user_id")).where(User.user_id == user_id)
        )
    if dept_ids:
        statements.append(
            select(user_depts.c.user_id.label("user_id")).where(
                user_depts.c.dept_id.in_(dept_ids)
            )
        )
    if not statements:
        statements.append(
            select(User.user_id.label("user_id")).where(User.user_id == user_id)
        )
    if len(statements) == 1:
        return statements[0]
    return union(*statements).subquery().select()


async def resolve_data_scope(
    db: AsyncSession,
    user: User,
) -> DataScopeResolution:
    """Resolve the union of every enabled role's concrete data scope."""
    if is_super_admin(user):
        return DataScopeResolution(
            scope_kinds=frozenset({DATA_SCOPE_ALL}),
            accessible_dept_ids=None,
            accessible_user_scope=None,
            include_self=True,
            unbounded=True,
        )

    roles = _enabled_roles(user)
    scope_kinds = frozenset(
        role.data_scope for role in roles if role.data_scope in KNOWN_DATA_SCOPES
    )
    if not scope_kinds:
        scope_kinds = frozenset({DATA_SCOPE_SELF})
    if DATA_SCOPE_ALL in scope_kinds:
        return DataScopeResolution(
            scope_kinds=scope_kinds,
            accessible_dept_ids=None,
            accessible_user_scope=None,
            include_self=True,
            unbounded=True,
        )

    own_dept_ids = {int(dept.dept_id) for dept in (user.depts or [])}
    accessible_dept_ids: set[int] = set()
    include_self = DATA_SCOPE_SELF in scope_kinds

    if DATA_SCOPE_DEPT in scope_kinds:
        accessible_dept_ids.update(own_dept_ids)
        include_self = include_self or not own_dept_ids

    if DATA_SCOPE_DEPT_AND_SUB in scope_kinds:
        subtree_ids = set(await get_dept_and_sub_ids(db, list(own_dept_ids)))
        accessible_dept_ids.update(subtree_ids)
        include_self = include_self or not subtree_ids

    custom_role_ids = {
        int(role.role_id) for role in roles if role.data_scope == DATA_SCOPE_CUSTOM
    }
    custom_by_role = await _custom_dept_ids_by_role(db, custom_role_ids)
    for dept_ids in custom_by_role.values():
        accessible_dept_ids.update(dept_ids)
        include_self = include_self or not dept_ids

    frozen_dept_ids = frozenset(accessible_dept_ids)
    return DataScopeResolution(
        scope_kinds=scope_kinds,
        accessible_dept_ids=frozen_dept_ids,
        accessible_user_scope=_build_user_scope(
            user_id=int(user.user_id),
            dept_ids=frozen_dept_ids,
            include_self=include_self,
        ),
        include_self=include_self,
        unbounded=False,
    )


def get_user_filters_from_resolution(
    resolution: DataScopeResolution,
) -> list:
    """Build the canonical User filter without resolving authorization twice."""
    if resolution.unbounded:
        return []
    assert resolution.accessible_user_scope is not None
    return [User.user_id.in_(resolution.accessible_user_scope)]


def get_best_scope(user: User) -> str:
    """从用户所有启用的角色中取最大权限范围"""
    priority = {
        DATA_SCOPE_ALL: 5,
        DATA_SCOPE_DEPT_AND_SUB: 4,
        DATA_SCOPE_DEPT: 3,
        DATA_SCOPE_CUSTOM: 2,
        DATA_SCOPE_SELF: 1,
    }
    best = DATA_SCOPE_SELF
    for role in user.roles:
        if role.status != "1":
            continue
        if priority.get(role.data_scope, 0) > priority.get(best, 0):
            best = role.data_scope
    return best


async def resolve_legacy_data_scope(
    db: AsyncSession,
    user: User,
) -> DataScopeResolution:
    """Reproduce the pre-Phase 2 traditional API scope for upgrade audits."""
    if is_super_admin(user):
        return DataScopeResolution(
            scope_kinds=frozenset({DATA_SCOPE_ALL}),
            accessible_dept_ids=None,
            accessible_user_scope=None,
            include_self=True,
            unbounded=True,
        )

    scope = get_best_scope(user)
    if scope == DATA_SCOPE_ALL:
        return DataScopeResolution(
            scope_kinds=frozenset({scope}),
            accessible_dept_ids=None,
            accessible_user_scope=None,
            include_self=True,
            unbounded=True,
        )

    own_dept_ids = {int(dept.dept_id) for dept in (user.depts or [])}
    accessible_dept_ids: set[int]
    include_self = scope == DATA_SCOPE_SELF
    if scope == DATA_SCOPE_CUSTOM:
        accessible_dept_ids = set(await get_custom_dept_ids(db, user))
        include_self = not accessible_dept_ids
    elif scope == DATA_SCOPE_DEPT:
        accessible_dept_ids = own_dept_ids
        include_self = not accessible_dept_ids
    elif scope == DATA_SCOPE_DEPT_AND_SUB:
        accessible_dept_ids = set(await get_dept_and_sub_ids(db, list(own_dept_ids)))
        include_self = not accessible_dept_ids
    else:
        accessible_dept_ids = set()
        include_self = True

    frozen_dept_ids = frozenset(accessible_dept_ids)
    return DataScopeResolution(
        scope_kinds=frozenset({scope}),
        accessible_dept_ids=frozen_dept_ids,
        accessible_user_scope=_build_user_scope(
            user_id=int(user.user_id),
            dept_ids=frozen_dept_ids,
            include_self=include_self,
        ),
        include_self=include_self,
        unbounded=False,
    )


async def resolve_legacy_ai_data_scope(
    db: AsyncSession,
    user: User,
) -> DataScopeResolution:
    """Reproduce the pre-Phase 2 AI DataScopeContext semantics."""
    if is_super_admin(user):
        return DataScopeResolution(
            scope_kinds=frozenset({DATA_SCOPE_ALL}),
            accessible_dept_ids=None,
            accessible_user_scope=None,
            include_self=True,
            unbounded=True,
        )

    scope = get_best_scope(user)
    if scope == DATA_SCOPE_ALL:
        return DataScopeResolution(
            scope_kinds=frozenset({scope}),
            accessible_dept_ids=None,
            accessible_user_scope=None,
            include_self=True,
            unbounded=True,
        )

    own_dept_ids = {int(dept.dept_id) for dept in (user.depts or [])}
    if scope == DATA_SCOPE_CUSTOM:
        accessible_dept_ids = set(await get_custom_dept_ids(db, user))
    elif scope == DATA_SCOPE_DEPT:
        accessible_dept_ids = own_dept_ids
    elif scope == DATA_SCOPE_DEPT_AND_SUB:
        accessible_dept_ids = set(await get_dept_and_sub_ids(db, list(own_dept_ids)))
    else:
        accessible_dept_ids = own_dept_ids

    frozen_dept_ids = frozenset(accessible_dept_ids)
    return DataScopeResolution(
        scope_kinds=frozenset({scope}),
        accessible_dept_ids=frozen_dept_ids,
        accessible_user_scope=_build_user_scope(
            user_id=int(user.user_id),
            dept_ids=frozen_dept_ids,
            include_self=True,
        ),
        include_self=True,
        unbounded=False,
    )


# 兼容旧调用方；新代码使用公开名称。
_get_best_scope = get_best_scope


async def get_data_scope_filters(
    db: AsyncSession,
    user: User,
    model: type,
    dept_field: str = "dept_id",
    user_field: str = "create_by",
) -> list:
    """
    根据用户角色的数据权限范围，生成 SQLAlchemy 过滤条件列表。

    适用于有 dept_id 直接字段的业务模型。

    Args:
        db: 数据库会话
        user: 当前用户
        model: 目标 SQLAlchemy 模型类
        dept_field: 模型中部门 ID 字段名
        user_field: 模型中创建人/用户标识字段名

    Returns:
        SQLAlchemy 过滤条件列表，为空列表表示不过滤。
    """
    resolution = await resolve_data_scope(db, user)
    if resolution.unbounded:
        return []
    dept_col = getattr(model, dept_field)
    user_col = getattr(model, user_field)
    conditions = []
    if resolution.accessible_dept_ids:
        conditions.append(dept_col.in_(resolution.accessible_dept_ids))
    if resolution.include_self:
        conditions.append(user_col == user.user_id)
    if not conditions:
        return [user_col == user.user_id]
    return [or_(*conditions)]


async def get_user_data_scope_filters(
    db: AsyncSession,
    current_user: User,
) -> list:
    """
    专用于 User 模型的数据权限过滤。

    User 模型通过 user_depts 多对多关联部门，需要子查询匹配。

    Args:
        db: 数据库会话
        current_user: 当前登录用户

    Returns:
        SQLAlchemy 过滤条件列表。
    """
    return get_user_filters_from_resolution(await resolve_data_scope(db, current_user))


async def get_custom_dept_ids(db: AsyncSession, user: User) -> list[int]:
    """获取用户角色通过 role_depts 关联的自定义部门 ID。

    过滤禁用部门（Dept.status != 1）—— admin 禁用部门 = 撤销 CUSTOM 授权。
    注：user_depts（用户自己的部门归属）和 ancestors 子树不过滤禁用部门，
    因为用户的组织归属不应被部门禁用剥夺（用户还在那个部门里管理）。
    """
    role_ids = [r.role_id for r in user.roles if r.status == STATUS_ENABLED]
    if not role_ids:
        return []
    stmt = (
        select(role_depts.c.dept_id)
        .join(Dept, Dept.dept_id == role_depts.c.dept_id)
        .where(
            role_depts.c.role_id.in_(role_ids),
            Dept.status == STATUS_ENABLED,
        )
    )
    result = await db.execute(stmt)
    return list(set(result.scalars().all()))


async def get_dept_and_sub_ids(db: AsyncSession, dept_ids: list[int]) -> list[int]:
    """获取指定部门及其所有子部门 ID（利用 ancestors 字段）。

    ancestors 字段是逗号分隔的父链（如 "0,12,123"）。用两端补逗号后 like
    锚定，避免数字子串误匹配（dept_id=12 不应匹配 ancestors="0,123"）。

    所有 dept_id 的 like 条件用 OR 合并成单次查询，避免 N+1。
    """
    if not dept_ids:
        return []
    conditions = [
        func.concat(",", Dept.ancestors, ",").like(f"%,{int(did)},%")
        for did in dept_ids
    ]
    stmt = select(Dept.dept_id).where(or_(*conditions))
    result = await db.execute(stmt)
    return list({*dept_ids, *result.scalars().all()})


# 兼容旧调用方；新代码使用公开名称。
_get_custom_dept_ids = get_custom_dept_ids
_get_dept_and_sub_ids = get_dept_and_sub_ids
