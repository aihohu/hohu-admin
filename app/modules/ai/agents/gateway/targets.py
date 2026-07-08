"""ensure_targets_in_scope — 数据鉴权 list 版 helper

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §6.2。

所有接受 *_id / *_ids 参数的 tool 必须在第一行调用，一次传全。
强制业务方在 user.update_dept(user_id, new_dept_id) 这种"双 ID"场景
一次传全（user_ids=[42], dept_ids=[8]），防遗漏。

None 表示"全部可见"（超管 / data_scope=DATA_SCOPE_ALL），跳过检查。
"""

from app.core.exceptions import AuthorizationException
from app.modules.ai.core.context import AiToolContext


def ensure_targets_in_scope(
    ctx: AiToolContext,
    *,
    user_ids: list[int] | None = None,
    dept_ids: list[int] | None = None,
    create_bys: list[int] | None = None,
) -> None:
    """检查目标 ID 是否在用户可见范围内

    Args:
        ctx: AiToolContext（含 data_scope）
        user_ids: 操作目标用户的 user_id 列表
        dept_ids: 操作目标部门的 dept_id 列表
        create_bys: 操作目标资源的 create_by user_id 列表

    Raises:
        AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION"):
            任一目标不在可见集合内

    注意：
        - ctx.data_scope.accessible_*_ids=None 表示"全部可见"，跳过检查
        - 空 list [] 视为"无目标"，跳过检查（业务方传空 list 是合法场景）
        - 集合判断 O(len(targets))，比 ColumnElement 拼子查询快得多
    """
    if user_ids is not None and ctx.data_scope.accessible_user_ids is not None:
        if not set(user_ids) <= ctx.data_scope.accessible_user_ids:
            raise AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION")

    if dept_ids is not None and ctx.data_scope.accessible_dept_ids is not None:
        if not set(dept_ids) <= ctx.data_scope.accessible_dept_ids:
            raise AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION")

    if create_bys is not None and ctx.data_scope.accessible_user_ids is not None:
        # create_by 是 user_id 维度，复用 accessible_user_ids
        if not set(create_bys) <= ctx.data_scope.accessible_user_ids:
            raise AuthorizationException(error_code="AI_DATA_SCOPE_VIOLATION")
