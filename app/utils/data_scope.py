"""数据权限过滤工具

根据用户角色的 data_scope 生成 SQLAlchemy 过滤条件，直接传入 paginate() 的 filters 参数。

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

from sqlalchemy import func, or_, select
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
from app.modules.system.models.user import User


def _get_best_scope(user: User) -> str:
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
    if is_super_admin(user):
        return []

    scope = _get_best_scope(user)

    if scope == DATA_SCOPE_ALL:
        return []

    user_dept_ids = [d.dept_id for d in user.depts]
    dept_col = getattr(model, dept_field)
    user_col = getattr(model, user_field)

    if scope == DATA_SCOPE_CUSTOM:
        dept_ids = await _get_custom_dept_ids(db, user)
        if dept_ids:
            return [dept_col.in_(dept_ids)]
        return [user_col == user.user_id]

    if scope == DATA_SCOPE_DEPT:
        if user_dept_ids:
            return [dept_col.in_(user_dept_ids)]
        return [user_col == user.user_id]

    if scope == DATA_SCOPE_DEPT_AND_SUB:
        dept_ids = await _get_dept_and_sub_ids(db, user_dept_ids)
        if dept_ids:
            return [dept_col.in_(dept_ids)]
        return [user_col == user.user_id]

    # DATA_SCOPE_SELF: 仅本人
    return [user_col == user.user_id]


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
    if is_super_admin(current_user):
        return []

    scope = _get_best_scope(current_user)

    if scope == DATA_SCOPE_ALL:
        return []

    current_dept_ids = [d.dept_id for d in current_user.depts]

    if scope == DATA_SCOPE_CUSTOM:
        dept_ids = await _get_custom_dept_ids(db, current_user)
        if not dept_ids:
            return [User.user_id == current_user.user_id]
    elif scope == DATA_SCOPE_DEPT:
        dept_ids = current_dept_ids if current_dept_ids else None
        if not dept_ids:
            return [User.user_id == current_user.user_id]
    elif scope == DATA_SCOPE_DEPT_AND_SUB:
        dept_ids = await _get_dept_and_sub_ids(db, current_dept_ids)
        if not dept_ids:
            return [User.user_id == current_user.user_id]
    else:
        # DATA_SCOPE_SELF
        return [User.user_id == current_user.user_id]

    # 子查询：在 user_depts 中有匹配 dept_id 的 user_id
    subquery = (
        select(user_depts.c.user_id)
        .where(user_depts.c.dept_id.in_(dept_ids))
        .correlate(User)
    )
    return [User.user_id.in_(subquery)]


async def _get_custom_dept_ids(db: AsyncSession, user: User) -> list[int]:
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


async def _get_dept_and_sub_ids(db: AsyncSession, dept_ids: list[int]) -> list[int]:
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
