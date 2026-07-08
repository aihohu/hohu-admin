"""system 模块的 AI 聚合 tool

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5.5 / §2.10。

三个聚合 tool：
  user.count    → 返回 {"count": N}，用于"有多少"类问题
  user.stats    → 返回 [{"group": ..., "count": ...}]，用于按维度分布
  user.distinct → 返回 ["v1", "v2"]，用于枚举字段取值

MVP 阶段 sys_user 表可聚合字段仅 status / user_gender（spec §2.10 / §5.5）。
dept_id / role_code 走关联表 EXISTS 子查询，留 v1.5。

注意：本模块 @ai_tool 装饰器执行期会把 tool 注册到 ToolRegistry，
启动时 ToolRegistry.validate_on_startup(db) 会校验 ai_agent 表里有
user_mgmt agent + system:user:list 权限码（spec §5.1）。
"""

from typing import Any

from sqlalchemy import func, select

from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.stats_validator import (
    validate_field_in_whitelist,
    validate_filters_in_whitelist,
    validate_group_by_in_whitelist,
)
from app.modules.ai.core.context import AiToolContext
from app.modules.system.models.user import User

# ============ user.count ============


@ai_tool(
    AiToolMeta(
        name="user.count",
        agent="user_mgmt",
        summary=(
            "Total user count → {'count': N}. For 'how many' / 'total'. "
            "NOT user.stats or user.distinct."
        ),
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status", "user_gender"),
        query_cache_module="system/user",
    )
)
async def user_count(ctx: AiToolContext, filters: dict[str, Any] | None = None) -> dict:
    """统计满足条件的用户数量，仅返回数字

    filters:
        status: '1' (启用) / '0' (禁用)
        user_gender: '0' (未知) / '1' (男) / '2' (女)
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

    # ctx.data_scope.filters 已含 User 模型的 data_scope 过滤（§6.2 build_data_scope_context）
    stmt = select(func.count(User.user_id)).where(*ctx.data_scope.filters)
    for key, value in filters.items():
        stmt = stmt.where(getattr(User, key) == value)

    count = await ctx.db.scalar(stmt)
    return {"count": int(count or 0)}


# ============ user.stats ============


@ai_tool(
    AiToolMeta(
        name="user.stats",
        agent="user_mgmt",
        summary=(
            "User distribution → [{group, count}]. For breakdown. "
            "NOT user.count or user.distinct."
        ),
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status", "user_gender"),
        allowed_group_by=("user_gender", "status"),
        max_groups=20,
    )
)
async def user_stats(
    ctx: AiToolContext,
    group_by: str | None = None,
    filters: dict[str, Any] | None = None,
) -> list[dict]:
    """按维度分组统计用户数量，返回 [{group, count}]

    group_by:
        user_gender: 男 / 女 / 未知（值 '1'/'2'/'0'）
        status: 启用 / 禁用（值 '1'/'0'）

    返回值按 count 降序，最多 max_groups 组（默认 20）。
    """
    group_by = validate_group_by_in_whitelist(ctx.tool_meta, group_by)
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

    col = getattr(User, group_by)
    stmt = (
        select(col, func.count(User.user_id))
        .where(*ctx.data_scope.filters)
        .group_by(col)
        .order_by(func.count(User.user_id).desc())
        .limit(ctx.tool_meta.max_groups)
    )
    for key, value in filters.items():
        stmt = stmt.where(getattr(User, key) == value)

    rows = (await ctx.db.execute(stmt)).all()
    return [{"group": str(g) if g is not None else "null", "count": c} for g, c in rows]


# ============ user.distinct ============


@ai_tool(
    AiToolMeta(
        name="user.distinct",
        agent="user_mgmt",
        summary=(
            "List distinct field values → ['1','0']. For 'which values'. "
            "NOT user.count or user.stats."
        ),
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        allowed_group_by=("user_gender", "status"),
        max_groups=50,
        query_cache_module="system/user",
    )
)
async def user_distinct(ctx: AiToolContext, field: str) -> list[str]:
    """枚举用户某字段的去重值

    field: user_gender / status（复用 allowed_group_by 作白名单，语义一致）
    """
    field = validate_field_in_whitelist(ctx.tool_meta, field)

    col = getattr(User, field)
    stmt = (
        select(col)
        .where(*ctx.data_scope.filters)
        .distinct()
        .limit(ctx.tool_meta.max_groups)
    )
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return [str(v) if v is not None else "null" for v in rows]
