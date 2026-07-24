"""build_data_scope_context — 把用户的角色 data_scope 转为 DataScopeContext

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §6.2 / §14 v1.5+。

为什么改成 subquery（v1.5+）：
  旧实现物化 set[int] 做 O(1) 集合包含检查，但单部门 5000+ 用户场景 OOM。
  v1.5+ 改成携带 SQL Select 子查询，ensure_targets_in_scope 走 EXISTS 验证目标。
  AI tool 调用上下文里多一次 10ms SQL 查询可忽略，但 OOM 风险根除。

  filters 仍走 ColumnElement 路径（stats tool / 业务函数拼 WHERE 用），保持透明。
"""

from sqlalchemy import Select, select, union
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
    """从 user 的角色 data_scope 构建 DataScopeContext

    Args:
        db: 任意 AsyncSession（chat endpoint 的 / tool 独立的）
        user: 当前登录用户（需已加载 roles / depts 关系）

    Returns:
        DataScopeContext:
          - accessible_dept_ids: None=全部可见；set[int]=部门集合（数量小，物化安全）
          - accessible_user_scope: None=全部可见；Select[tuple[int]]=返可见 user_id
            的 SQL 子查询，ensure_targets_in_scope 走 EXISTS 验证
          - filters: User 模型的 data_scope filter（最常见 stats 目标）；
                     其它模型 stats tool 在函数内自行调 get_data_scope_filters
    """
    if is_super_admin(user):
        return DataScopeContext(
            accessible_dept_ids=None,
            accessible_user_scope=None,
            filters=[],
        )

    # User 模型 filter（公开 API，复用现有 user_depts 子查询逻辑）
    user_filters = await get_user_data_scope_filters(db, user)

    scope = get_best_scope(user)
    if scope == DATA_SCOPE_ALL:
        return DataScopeContext(
            accessible_dept_ids=None,
            accessible_user_scope=None,
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

    # user 维度子查询：SELF = 仅自己；其它 = user_depts 反查 + 自己
    # 用 union 把"自己"和"部门关联的用户"合并（spec §6.2：当前用户始终可见）
    # label("user_id") 保留列名，下游 targets.py::ensure_targets_in_scope 通过
    # subq.c.user_id 引用（union 后默认列名可能丢失）
    own_user_stmt = select(User.user_id.label("user_id")).where(
        User.user_id == user.user_id
    )
    if scope == DATA_SCOPE_SELF:
        user_scope: Select[tuple[int]] = own_user_stmt
    else:
        dept_user_stmt = select(user_depts.c.user_id.label("user_id")).where(
            user_depts.c.dept_id.in_(dept_ids)
        )
        user_scope = union(own_user_stmt, dept_user_stmt).subquery().select()

    return DataScopeContext(
        accessible_dept_ids=dept_ids,
        accessible_user_scope=user_scope,
        filters=list(user_filters),
    )
