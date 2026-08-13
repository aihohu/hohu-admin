"""ensure_targets_in_scope — 数据鉴权 list 版 helper

使用数据权限子查询验证工具目标是否对当前用户可见。

所有接受 *_id / *_ids 参数的 tool 必须在第一行调用，一次传全。
强制业务方在 user.update_dept(user_id, new_dept_id) 这种"双 ID"场景
一次传全（user_ids=[42], dept_ids=[8]），防遗漏。

None 表示"全部可见"（超管 / data_scope=DATA_SCOPE_ALL），跳过检查。

当前实现：
  accessible_user_scope 从 set[int] 物化改为 SQL Select 子查询，避免大部门 OOM。
  本函数因此改成 async，user_ids / create_bys 走 SQL count(*) 路径验证目标。
  dept_ids 数量小，保留 set 内存检查（同步）。
"""

from sqlalchemy import Select, func, select

from app.core.exceptions import AuthorizationException
from app.modules.ai.core.context import AiToolContext


async def ensure_targets_in_scope(
    ctx: AiToolContext,
    *,
    user_ids: list[int] | None = None,
    dept_ids: list[int] | None = None,
    create_bys: list[int] | None = None,
) -> None:
    """异步检查目标 ID 是否在用户可见范围内。

    Args:
        ctx: AiToolContext（含 data_scope + db）
        user_ids: 操作目标用户的 user_id 列表
        dept_ids: 操作目标部门的 dept_id 列表
        create_bys: 操作目标资源的 create_by user_id 列表

    Raises:
        AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION"):
            任一目标不在可见集合内

    注意：
        - ctx.data_scope.accessible_user_scope=None 表示"全部可见"，跳过 user 检查
        - 空 list [] 视为"无目标"，跳过检查
        - user_ids / create_bys 走 SQL count（10ms 级，AI 上下文可忽略）
        - dept_ids 仍走 set 内存检查（部门数量小）
    """
    # user_ids 和 create_bys 都通过 accessible_user_scope 子查询验证。
    targets: set[int] = set()
    if user_ids:
        targets.update(user_ids)
    if create_bys:
        targets.update(create_bys)

    if targets and ctx.data_scope.accessible_user_scope is not None:
        await _ensure_users_in_scope(
            ctx.data_scope.accessible_user_scope,
            list(targets),
            ctx,
        )

    if dept_ids and ctx.data_scope.accessible_dept_ids is not None:
        if not set(dept_ids) <= ctx.data_scope.accessible_dept_ids:
            raise AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION")


async def _ensure_users_in_scope(
    scope: Select[tuple[int]],
    targets: list[int],
    ctx: AiToolContext,
) -> None:
    """验证 targets 全部在 scope 内（SQL count 路径）。

    SQL：SELECT count(*) FROM (<scope>) WHERE user_id IN (:t1, :t2, ...)
    若 count < len(targets) 说明有越界目标。
    """
    subq = scope.subquery("visible_users")
    count_stmt = (
        select(func.count()).select_from(subq).where(subq.c.user_id.in_(targets))
    )
    visible = (await ctx.db.execute(count_stmt)).scalar_one()
    if visible < len(targets):
        raise AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION")
