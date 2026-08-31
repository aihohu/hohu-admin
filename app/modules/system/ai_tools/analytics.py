"""Bounded aggregate tools for System domains."""

from typing import Any

from sqlalchemy import func, select

from app.modules.ai.agents.gateway.result import (
    ToolResult,
    UIResult,
)
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.stats_validator import (
    validate_field_in_whitelist,
    validate_filters_in_whitelist,
    validate_group_by_in_whitelist,
)
from app.modules.ai.core.context import AiToolContext
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.dept_selector import department_selector

from .common import (
    _result_projection,
    _validate_enable_status_filter,
)

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
        idempotent=True,
        allowed_filters=("status", "user_gender"),
        chip_target="/system/user",
    )
)
async def user_count(
    ctx: AiToolContext, filters: dict[str, Any] | None = None
) -> ToolResult:
    """统计满足条件的用户数量，仅返回数字

    filters:
        status: '1' (启用) / '2' (禁用)
        user_gender: '0' (未知) / '1' (男) / '2' (女)

    状态码必须使用字符串，避免数字强制转换造成机器契约歧义。
    """
    filters = _validate_enable_status_filter(
        validate_filters_in_whitelist(ctx.tool_meta, filters)
    )

    # data_scope 已收敛到调用者可见的用户范围。
    stmt = select(func.count(User.user_id)).where(*ctx.data_scope.filters)
    for key, value in filters.items():
        # sys_user 表的 allowed_filters 字段都是 varchar，强制 stringify 防类型错
        stmt = stmt.where(getattr(User, key) == str(value))

    count = int(await ctx.db.scalar(stmt) or 0)
    return ToolResult.success(
        data={"count": count},
        projection=_result_projection(scope_bound=True),
        ui=UIResult(
            view_type="plain_json",
            view_data={"count": count},
            audit={"count": count},
            label_key="ai.tool.user.count.result",
            label_params={"count": count},
        ),
    )


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
        idempotent=True,
        allowed_filters=("status", "user_gender"),
        allowed_group_by=("user_gender", "status"),
        max_groups=20,
        result_view="stats_chart",
        chip_target="/system/user",
    )
)
async def user_stats(
    ctx: AiToolContext,
    group_by: str | None = None,
    filters: dict[str, Any] | None = None,
) -> ToolResult:
    """按维度分组统计用户数量，返回 [{group, count}]

    group_by:
        user_gender: 男 / 女 / 未知（值 '1'/'2'/'0'）
        status: 启用 / 禁用（值 '1'/'2'）

    返回值按 count 降序，最多 max_groups 组（默认 20）。
    """
    group_by = validate_group_by_in_whitelist(ctx.tool_meta, group_by)
    filters = _validate_enable_status_filter(
        validate_filters_in_whitelist(ctx.tool_meta, filters)
    )

    col = getattr(User, group_by)
    stmt = (
        select(col, func.count(User.user_id))
        .where(*ctx.data_scope.filters)
        .group_by(col)
        .order_by(func.count(User.user_id).desc())
        .limit(ctx.tool_meta.max_groups)
    )
    for key, value in filters.items():
        # sys_user 表字段都是 varchar，强制 stringify 防类型错（与 user_count 同）
        stmt = stmt.where(getattr(User, key) == str(value))

    rows = (await ctx.db.execute(stmt)).all()
    groups = [
        {"group": str(g) if g is not None else "null", "count": c} for g, c in rows
    ]
    return ToolResult.success(
        data={"groups": groups},
        projection=_result_projection(scope_bound=True),
        ui=UIResult(
            view_type="stats_chart",
            view_data={"rows": groups},
            audit={"total": sum(g["count"] for g in groups)},
            label_key="ai.tool.user.stats.result",
        ),
    )


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
        idempotent=True,
        allowed_group_by=("user_gender", "status"),
        max_groups=50,
        chip_target="/system/user",
    )
)
async def user_distinct(ctx: AiToolContext, field: str) -> ToolResult:
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
    values = [str(v) if v is not None else "null" for v in rows]
    return ToolResult.success(
        data={"values": values},
        projection=_result_projection(scope_bound=True),
        ui=UIResult(
            view_type="plain_json",
            view_data={"values": values},
            audit={"count": len(values)},
            label_key="ai.tool.user.distinct.result",
            label_params={"count": len(values)},
        ),
    )


# ============ role.count ============


@ai_tool(
    AiToolMeta(
        name="role.count",
        agent="role_mgmt",
        summary=(
            "Total role count → {'count': N}. For 'how many roles'. "
            "Status filter: '1' enabled / '2' disabled."
        ),
        required_perms=("system:role:list",),
        risk="low",
        readonly=True,
        idempotent=True,
        allowed_filters=("status",),
        chip_target="/system/role",
    )
)
async def role_count(
    ctx: AiToolContext, filters: dict[str, Any] | None = None
) -> ToolResult:
    """统计角色数量，仅返回数字

    filters:
        status: '1' (启用) / '2' (禁用)
    """
    filters = _validate_enable_status_filter(
        validate_filters_in_whitelist(ctx.tool_meta, filters)
    )

    stmt = select(func.count(Role.role_id))
    for key, value in filters.items():
        # sys_role 表字段都是 varchar，强制 stringify 防类型错
        stmt = stmt.where(getattr(Role, key) == str(value))

    count = int(await ctx.db.scalar(stmt) or 0)
    return ToolResult.success(
        data={"count": count},
        projection=_result_projection(scope_bound=True),
        ui=UIResult(
            view_type="plain_json",
            view_data={"count": count},
            audit={"count": count},
            label_key="ai.tool.role.count.result",
            label_params={"count": count},
        ),
    )


# ============ dept.count ============


@ai_tool(
    AiToolMeta(
        name="dept.count",
        agent="dept_mgmt",
        summary="Total department count → {'count': N}. For 'how many departments'.",
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        idempotent=True,
        allowed_filters=("status",),
        chip_target="/system/dept",
    )
)
async def dept_count(
    ctx: AiToolContext, filters: dict[str, Any] | None = None
) -> ToolResult:
    """统计部门数量，仅返回数字

    filters:
        status: '1' (启用) / '2' (禁用)
    """
    filters = _validate_enable_status_filter(
        validate_filters_in_whitelist(ctx.tool_meta, filters)
    )

    scoped_filters = []
    for key, value in filters.items():
        # sys_dept 表字段都是 varchar，强制 stringify 防类型错
        scoped_filters.append(getattr(Dept, key) == str(value))

    count = await department_selector.count(
        ctx.db,
        scope=ctx.data_scope,
        filters=scoped_filters,
    )
    return ToolResult.success(
        data={"count": count},
        projection=_result_projection(scope_bound=True),
        ui=UIResult(
            view_type="plain_json",
            view_data={"count": count},
            audit={"count": count},
            label_key="ai.tool.dept.count.result",
            label_params={"count": count},
        ),
    )
