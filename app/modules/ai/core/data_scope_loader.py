"""build_data_scope_context — 把用户的角色 data_scope 转为 DataScopeContext

构造用于 AI 工具目标校验的数据权限子查询。

为什么使用 subquery：
  旧实现物化 set[int] 做 O(1) 集合包含检查，但单部门 5000+ 用户场景 OOM。
  当前携带 SQL Select 子查询，由 ensure_targets_in_scope 使用 EXISTS 验证目标。
  AI tool 调用上下文里多一次 10ms SQL 查询可忽略，但 OOM 风险根除。

  filters 仍走 ColumnElement 路径（stats tool / 业务函数拼 WHERE 用），保持透明。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.system.models.user import User
from app.utils.data_scope import (
    get_user_filters_from_resolution,
    resolve_data_scope,
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
    resolution = await resolve_data_scope(db, user)

    return DataScopeContext(
        accessible_dept_ids=(
            None
            if resolution.accessible_dept_ids is None
            else set(resolution.accessible_dept_ids)
        ),
        accessible_user_scope=resolution.accessible_user_scope,
        filters=get_user_filters_from_resolution(resolution),
        scope_kinds=resolution.scope_kinds,
    )
