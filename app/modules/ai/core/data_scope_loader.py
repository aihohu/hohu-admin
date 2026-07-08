"""build_data_scope_context — 把用户的角色 data_scope 物化为 DataScopeContext

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §6.2。

为什么需要物化：
  现有 get_data_scope_filters(db, user, model) 返回 ColumnElement 列表，
  stats tool 可直接拼到 WHERE 子句（§5.5）；但 ensure_targets_in_scope 需要
  set[int] 做"目标 ⊆ 可见集合"判断，ColumnElement 做不到。

  本 helper 同时填两套表征：
    accessible_*_ids → ensure_targets_in_scope 用（O(1) 集合判断）
    filters          → stats tool / 业务函数直接拼 WHERE 用

  ⚠️ 大租户警告：单部门 5000+ 用户时 accessible_user_ids 集合很大（spec §15），
  v1.5+ 改 set[int] | Literal["subquery"] + EXISTS 查询。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
)
from app.core.rbac import is_super_admin
from app.db.base import user_depts
from app.modules.system.models.user import User
from app.utils.data_scope import (
    get_best_scope,
    get_custom_dept_ids,
    get_dept_and_sub_ids,
    get_user_data_scope_filters,
)

from .context import DataScopeContext


async def build_data_scope_context(
    db: AsyncSession,
    user: User,
) -> DataScopeContext:
    """从 user 的角色 data_scope 物化 DataScopeContext

    Args:
        db: 任意 AsyncSession（chat endpoint 的 / tool 独立的）
        user: 当前登录用户（需已加载 roles / depts 关系）

    Returns:
        DataScopeContext:
          - accessible_dept_ids / accessible_user_ids: None = 全部可见（超管 / ALL scope）
          - filters: User 模型的 data_scope filter（最常见 stats 目标）；
                     其它模型 stats tool 在函数内自行调 get_data_scope_filters
    """
    if is_super_admin(user):
        return DataScopeContext(
            accessible_dept_ids=None,
            accessible_user_ids=None,
            filters=[],
        )

    # User 模型 filter（公开 API，复用现有 user_depts 子查询逻辑）
    user_filters = await get_user_data_scope_filters(db, user)

    scope = get_best_scope(user)
    if scope == DATA_SCOPE_ALL:
        return DataScopeContext(
            accessible_dept_ids=None,
            accessible_user_ids=None,
            filters=[],
        )

    user_dept_ids = [d.dept_id for d in user.depts]

    if scope == DATA_SCOPE_CUSTOM:
        dept_ids = set(await get_custom_dept_ids(db, user))
    elif scope == DATA_SCOPE_DEPT:
        dept_ids = set(user_dept_ids)
    elif scope == DATA_SCOPE_DEPT_AND_SUB:
        dept_ids = set(await get_dept_and_sub_ids(db, user_dept_ids))
    else:  # DATA_SCOPE_SELF
        # SELF scope：仍给 dept 视图（用户自己所在部门），但 user 维度收敛到自己
        dept_ids = set(user_dept_ids)

    # user 维度：SELF = {自己}；其它 = 通过 user_depts 关联表反查（User 无 dept_id 字段）
    if scope == DATA_SCOPE_SELF:
        user_ids: set[int] = {user.user_id}
    else:
        # ⚠️ 大租户警告：单部门 5000+ 用户时此集合很大，见 spec §15
        stmt = select(user_depts.c.user_id).where(user_depts.c.dept_id.in_(dept_ids))
        rows = (await db.execute(stmt)).scalars().all()
        user_ids = set(rows) | {user.user_id}

    return DataScopeContext(
        accessible_dept_ids=dept_ids,
        accessible_user_ids=user_ids,
        filters=list(user_filters),
    )
