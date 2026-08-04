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

from typing import Any, Literal

from sqlalchemy import func, select

from app.modules.ai.agents.gateway import ensure_targets_in_scope
from app.modules.ai.agents.gateway.result import ToolResult, UIResult
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
        chip_target="/system/user",
    )
)
async def user_count(
    ctx: AiToolContext, filters: dict[str, Any] | None = None
) -> ToolResult:
    """统计满足条件的用户数量，仅返回数字

    filters:
        status: '1' (启用) / '0' (禁用)
        user_gender: '0' (未知) / '1' (男) / '2' (女)

    注意：LLM 经常以 JSON int 传值（filters={"status": 1}），sys_user 字段是
    varchar，asyncpg 严格类型检查会抛 ProgrammingError。这里强制 stringify。
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

    # ctx.data_scope.filters 已含 User 模型的 data_scope 过滤（§6.2 build_data_scope_context）
    stmt = select(func.count(User.user_id)).where(*ctx.data_scope.filters)
    for key, value in filters.items():
        # sys_user 表的 allowed_filters 字段都是 varchar，强制 stringify 防类型错
        stmt = stmt.where(getattr(User, key) == str(value))

    count = int(await ctx.db.scalar(stmt) or 0)
    return ToolResult.success(
        data={"count": count},
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
        # sys_user 表字段都是 varchar，强制 stringify 防类型错（与 user_count 同）
        stmt = stmt.where(getattr(User, key) == str(value))

    rows = (await ctx.db.execute(stmt)).all()
    groups = [
        {"group": str(g) if g is not None else "null", "count": c} for g, c in rows
    ]
    return ToolResult.success(
        data={"groups": groups},
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
        ui=UIResult(
            view_type="plain_json",
            view_data={"values": values},
            audit={"count": len(values)},
            label_key="ai.tool.user.distinct.result",
            label_params={"count": len(values)},
        ),
    )


# ============ role.count（v1.5+，复用 user.count 模式，演示 chip 跳转回放到 role 模块页） ============


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
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

    stmt = select(func.count(Role.role_id))
    for key, value in filters.items():
        # sys_role 表字段都是 varchar，强制 stringify 防类型错
        stmt = stmt.where(getattr(Role, key) == str(value))

    count = int(await ctx.db.scalar(stmt) or 0)
    return ToolResult.success(
        data={"count": count},
        ui=UIResult(
            view_type="plain_json",
            view_data={"count": count},
            audit={"count": count},
            label_key="ai.tool.role.count.result",
            label_params={"count": count},
        ),
    )


# ============ dept.count（v1.5+，演示 chip 跳转回放到 dept 模块页） ============


@ai_tool(
    AiToolMeta(
        name="dept.count",
        agent="dept_mgmt",
        summary="Total department count → {'count': N}. For 'how many departments'.",
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        chip_target="/system/dept",
    )
)
async def dept_count(
    ctx: AiToolContext, filters: dict[str, Any] | None = None
) -> ToolResult:
    """统计部门数量，仅返回数字

    filters:
        status: '1' (启用) / '0' (禁用)
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

    stmt = select(func.count(Dept.dept_id))
    for key, value in filters.items():
        # sys_dept 表字段都是 varchar，强制 stringify 防类型错
        stmt = stmt.where(getattr(Dept, key) == str(value))

    count = int(await ctx.db.scalar(stmt) or 0)
    return ToolResult.success(
        data={"count": count},
        ui=UIResult(
            view_type="plain_json",
            view_data={"count": count},
            audit={"count": count},
            label_key="ai.tool.dept.count.result",
            label_params={"count": count},
        ),
    )


# ============ role.list / dept.list（v1.5+ SR-22，LLM 需少量行而非仅 count） ============

# 返回字段精简（id/name/code/status），phone/email/create_by 等不进 records（§7.3 兜底剥离）
_LIST_MAX_LIMIT = 50  # 强制上限，防大客户 OOM + LLM token 爆炸（SR-22 反例 3）
_LIST_DEFAULT_LIMIT = 20


def _coerce_list_limit(limit: int | None) -> int:
    """规范化 limit：None=默认 20；负数=默认；>50=截断到 50"""
    if limit is None or limit <= 0:
        return _LIST_DEFAULT_LIMIT
    return min(limit, _LIST_MAX_LIMIT)


@ai_tool(
    AiToolMeta(
        name="role.list",
        agent="role_mgmt",
        summary=(
            "List roles → {total, limit, sample[3]}. Frontend renders data_list. "
            "Use role.count for count-only."
        ),
        required_perms=("system:role:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        chip_target="/system/role",
        result_view="data_list",
    )
)
async def role_list(
    ctx: AiToolContext,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
) -> ToolResult:
    """列出角色，返回前 N 条精简字段

    LLM 看 data.{total, limit, sample[3]}（精简，进 prompt cache）；
    前端看 ui.view_data.{columns, rows}（全量 limit 条，渲染 table）。

    filters:
        status: '1' (启用) / '2' (禁用)
    limit:
        None / 0 / 负数 = 默认 20；正整数按 min(limit, 50) 截断
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)
    safe_limit = _coerce_list_limit(limit)

    base = select(Role)
    for key, value in filters.items():
        base = base.where(getattr(Role, key) == str(value))

    # total 反映真实总数（不受 limit 截断），供 LLM 判断是否需 chip 跳转
    total = int(
        await ctx.db.scalar(select(func.count()).select_from(base.subquery())) or 0
    )

    rows = (
        (await ctx.db.execute(base.order_by(Role.role_id.asc()).limit(safe_limit)))
        .scalars()
        .all()
    )

    columns = [
        {"key": "id", "label": "ID"},
        {"key": "name", "label": "名称"},
        {"key": "code", "label": "编码"},
        {"key": "status", "label": "状态"},
    ]
    records = [
        {
            "id": str(r.role_id),
            "name": r.role_name,
            "code": r.role_code,
            "status": r.status,
        }
        for r in rows
    ]
    return ToolResult.success(
        data={
            "total": total,
            "limit": safe_limit,
            "sample": records[:3],  # 给 LLM 看前 3 条（prompt cache 友好）
        },
        ui=UIResult(
            view_type="data_list",
            view_data={"columns": columns, "rows": records},
            audit={"total": total},
            label_key="ai.tool.role.list.result",
            label_params={"count": total},
        ),
    )


@ai_tool(
    AiToolMeta(
        name="dept.list",
        agent="dept_mgmt",
        summary=(
            "List depts → {total, limit, sample[3]}. Frontend renders data_list. "
            "Use dept.count for count-only."
        ),
        required_perms=("system:dept:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status",),
        chip_target="/system/dept",
        result_view="data_list",
    )
)
async def dept_list(
    ctx: AiToolContext,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
) -> ToolResult:
    """列出部门，返回前 N 条精简字段

    LLM 看 data.{total, limit, sample[3]}（精简，进 prompt cache）；
    前端看 ui.view_data.{columns, rows}（全量 limit 条，渲染 table）。

    filters:
        status: '1' (启用) / '0' (禁用)
    limit:
        None / 0 / 负数 = 默认 20；正整数按 min(limit, 50) 截断
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)
    safe_limit = _coerce_list_limit(limit)

    base = select(Dept)
    for key, value in filters.items():
        base = base.where(getattr(Dept, key) == str(value))

    total = int(
        await ctx.db.scalar(select(func.count()).select_from(base.subquery())) or 0
    )

    rows = (
        (await ctx.db.execute(base.order_by(Dept.dept_id.asc()).limit(safe_limit)))
        .scalars()
        .all()
    )

    columns = [
        {"key": "id", "label": "ID"},
        {"key": "name", "label": "名称"},
        {"key": "parent_id", "label": "父部门 ID"},
        {"key": "status", "label": "状态"},
    ]
    records = [
        {
            "id": str(d.dept_id),
            "name": d.dept_name,
            "parent_id": str(d.parent_id) if d.parent_id else None,
            "status": d.status,
        }
        for d in rows
    ]
    return ToolResult.success(
        data={
            "total": total,
            "limit": safe_limit,
            "sample": records[:3],
        },
        ui=UIResult(
            view_type="data_list",
            view_data={"columns": columns, "rows": records},
            audit={"total": total},
            label_key="ai.tool.dept.list.result",
            label_params={"count": total},
        ),
    )


# ============ user.batch_delete（destructive + HITL，spec §11.3 示例） ============


async def _resolve_users(
    ctx: AiToolContext,
    *,
    user_ids: list[int] | None,
    user_names: list[str] | None,
    phones: list[str] | None,
) -> list[User]:
    """按 IDs / 用户名 / 手机号解析为 User 列表（spec §6.2 data_scope 强制）。

    三个选择器至少提供一个；同时提供则取并集（任一匹配即纳入）。
    始终应用 ctx.data_scope.filters，确保只返回 caller 可见范围内的用户。
    user_name / phone 精确匹配（避免误删同名前缀用户）；多重匹配全部纳入，
    dry_run 阶段 HITL 抽屉展示全部匹配项供用户决定。
    """
    from sqlalchemy import or_  # noqa: PLC0415

    from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

    if not user_ids and not user_names and not phones:
        raise BusinessRuleException(
            "至少提供 user_ids / user_names / phones 中的一个",
            error_code="AI_BATCH_DELETE_NO_TARGETS",
        )

    clauses = []
    if user_ids:
        clauses.append(User.user_id.in_(user_ids))
    if user_names:
        clauses.append(User.user_name.in_(user_names))
    if phones:
        clauses.append(User.user_phone.in_(phones))

    stmt = select(User).where(*ctx.data_scope.filters, or_(*clauses))
    return list((await ctx.db.execute(stmt)).scalars().all())


@ai_tool(
    AiToolMeta(
        name="user.batch_delete",
        agent="user_mgmt",
        summary=(
            "Delete users by IDs / names / phones → {'deleted': N}. "
            "HITL confirms; ambiguous deletes all."
        ),
        required_perms=("system:user:delete",),
        risk="destructive",
        hitl_always=True,
        dry_run_supported=True,
        result_view="rows_affected",
    )
)
async def user_batch_delete(
    ctx: AiToolContext,
    *,
    user_ids: list[int] | None = None,
    user_names: list[str] | None = None,
    phones: list[str] | None = None,
) -> ToolResult:
    """Delete users by their identifiers.

    Call this tool immediately when the user requests deletion — the HITL
    (Human-In-The-Loop) confirmation drawer is shown to the user automatically
    by the backend; you should NOT ask the user to confirm via chat text.

    At least one of user_ids / user_names / phones must be provided. Multiple
    selectors are unioned. For natural-language requests like "删除张三",
    pass user_names=["张三"]; the dry_run drawer lists all matches so the
    user can verify before confirming.

    Args:
        user_ids: Snowflake int64 IDs (when caller already knows them)
        user_names: user_name exact matches (for "by name" requests)
        phones: phone exact matches (for "by phone" requests)
    """
    users = await _resolve_users(
        ctx, user_ids=user_ids, user_names=user_names, phones=phones
    )
    if not users:
        from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

        raise BusinessRuleException(
            "未找到匹配用户（字段值不匹配 / 不在可见范围 / 已删除）",
            error_code="AI_BATCH_DELETE_NO_MATCH",
        )

    resolved_ids = [u.user_id for u in users]
    # spec §6.2 data_scope 强制：写 tool 含 *_ids 必须先验证 targets 可见。
    # _resolve_users 已应用 data_scope.filters，此处 defensive 二次校验。
    await ensure_targets_in_scope(ctx, user_ids=resolved_ids)

    from app.modules.system.service.user_service import user_service  # noqa: PLC0415

    count = await user_service.batch_delete_users(
        ctx.db, resolved_ids, current_user_id=ctx.user.user_id
    )
    # spec 2026-07-16 §2.4: LLM 只看 {"deleted": N}（user_ids 不进 prompt cache），
    # 受影响 IDs 进 ui.view_data.ids + ui.audit.affected_user_ids（后台审计页反查）。
    str_ids = [str(i) for i in resolved_ids]
    return ToolResult.success(
        data={"deleted": count},
        ui=UIResult(
            view_type="rows_affected",
            view_data={"count": count, "ids": str_ids},
            audit={"affected_user_ids": str_ids},
            label_key="ai.tool.user.batch_delete.result",
            label_params={"count": count},
        ),
    )


async def _dry_run_user_batch_delete(
    ctx: AiToolContext,
    *,
    user_ids: list[int] | None = None,
    user_names: list[str] | None = None,
    phones: list[str] | None = None,
) -> Any:
    """dry_run：解析 IDs/names/phones → 列出匹配用户供 HITL 抽屉确认"""
    from app.core.exceptions import (  # noqa: PLC0415
        AuthorizationException,
        BusinessRuleException,
    )
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    try:
        users = await _resolve_users(
            ctx, user_ids=user_ids, user_names=user_names, phones=phones
        )
    except BusinessRuleException as e:
        return DryRunResult(ok=False, count=0, reason=e.message)

    if not users:
        return DryRunResult(
            ok=False,
            count=0,
            reason="未找到匹配用户（字段值不匹配 / 不在可见范围 / 已删除）",
        )

    resolved_ids = [u.user_id for u in users]
    try:
        # spec §6.2: dry_run 也要校验 data_scope（防越权预估）
        await ensure_targets_in_scope(ctx, user_ids=resolved_ids)
    except AuthorizationException as e:
        return DryRunResult(ok=False, count=0, reason=e.message)

    examples = [
        f"{u.user_name}（ID: {u.user_id}, phone: {u.user_phone or '-'}）"
        for u in users[:10]
    ]
    summary = (
        f"将删除 {len(users)} 个用户："
        f"{', '.join(u.user_name for u in users[:3])}"
        f"{'...' if len(users) > 3 else ''}"
    )
    return DryRunResult(
        ok=True,
        count=len(users),
        reason=summary,
        examples=examples,
    )


# ============ user.list / user.lookup / user.update（spec §10 Task 23-25） ============


@ai_tool(
    AiToolMeta(
        name="user.list",
        agent="user_mgmt",
        summary=(
            "List users → {total, limit, sample[3]}. Frontend renders data_list. "
            "Use user.count for count-only."
        ),
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        allowed_filters=("status", "user_gender"),
        chip_target="/system/user",
        result_view="data_list",
    )
)
async def user_list(
    ctx: AiToolContext,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
) -> ToolResult:
    """列出用户，返回前 N 条精简字段（spec §10 Task 23）

    LLM 看 data.{total, limit, sample[3]}（精简，进 prompt cache）；
    前端看 ui.view_data.{columns, rows}（全量 limit 条，渲染 table）。

    filters:
        status: '1' (启用) / '2' (禁用)
        user_gender: '0' (未知) / '1' (男) / '2' (女)
    limit:
        None / 0 / 负数 = 默认 20；正整数按 min(limit, 50) 截断
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)
    safe_limit = _coerce_list_limit(limit)

    base = select(User)
    for key, value in filters.items():
        # sys_user 表字段都是 varchar，强制 stringify 防类型错
        base = base.where(getattr(User, key) == str(value))

    # spec §6.2 data_scope 强制：read tool 也走 ctx.data_scope.filters
    base = base.where(*ctx.data_scope.filters)

    total = int(
        await ctx.db.scalar(select(func.count()).select_from(base.subquery())) or 0
    )

    rows = (
        (await ctx.db.execute(base.order_by(User.user_id.asc()).limit(safe_limit)))
        .scalars()
        .all()
    )

    columns = [
        {"key": "id", "label": "ID"},
        {"key": "user_name", "label": "用户名"},
        {"key": "nickname", "label": "昵称"},
        {"key": "status", "label": "状态"},
    ]
    records = [
        {
            "id": str(u.user_id),
            "user_name": u.user_name,
            "nickname": u.nickname or "",
            "status": u.status,
        }
        for u in rows
    ]
    return ToolResult.success(
        data={
            "total": total,
            "limit": safe_limit,
            "sample": records[:3],  # 给 LLM 看前 3 条
        },
        ui=UIResult(
            view_type="data_list",
            view_data={"columns": columns, "rows": records},
            audit={"total": total},
            label_key="ai.tool.user.list.result",
            label_params={"count": total},
        ),
    )


@ai_tool(
    AiToolMeta(
        name="user.lookup",
        agent="user_mgmt",
        summary=(
            "Lookup single user by id/name/phone/email → detail_card. "
            "NOT for listing — use user.list."
        ),
        required_perms=("system:user:list",),
        risk="low",
        readonly=True,
        result_view="detail_card",
    )
)
async def user_lookup(
    ctx: AiToolContext,
    *,
    user_id: int | None = None,
    user_name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> ToolResult:
    """查询单个用户详情（spec §10 Task 24）

    至少提供一个 selector；多 selector 取交集（AND）；
    user_name / phone / email 精确匹配（避免误匹配前缀）。
    始终应用 ctx.data_scope.filters，确保只返回 caller 可见范围内的用户。

    Args:
        user_id: Snowflake int64 ID（最精准）
        user_name: 登录账号精确匹配
        phone: 手机号精确匹配
        email: 邮箱精确匹配
    """
    from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

    if not user_id and not user_name and not phone and not email:
        raise BusinessRuleException(
            "至少提供 user_id / user_name / phone / email 中的一个",
            error_code="AI_LOOKUP_NO_TARGET",
        )

    # spec §6.2 data_scope 强制：read tool 含 user_id 也需校验 target 可见
    # （防越权预估：用户传 admin 的 user_id 直接 lookup 拿到敏感字段）
    if user_id is not None:
        await ensure_targets_in_scope(ctx, user_ids=[user_id])

    stmt = select(User).where(*ctx.data_scope.filters)
    if user_id is not None:
        stmt = stmt.where(User.user_id == user_id)
    if user_name is not None:
        stmt = stmt.where(User.user_name == user_name)
    if phone is not None:
        stmt = stmt.where(User.user_phone == phone)
    if email is not None:
        stmt = stmt.where(User.user_email == email)

    user = (await ctx.db.execute(stmt.limit(2))).scalars().all()
    if not user:
        raise BusinessRuleException(
            "未找到匹配用户（字段值不匹配 / 不在可见范围 / 已删除）",
            error_code="AI_LOOKUP_NO_MATCH",
        )
    if len(user) > 1:
        # 多重匹配：返回首个 + warning hint，让 LLM 主动反问用户细化（spec §8.6）
        # 不抛异常，因为多重匹配在 lookup 场景不致命（仅是 ambiguous）
        pass

    u = user[0]
    return ToolResult.success(
        data={
            "id": str(u.user_id),
            "user_name": u.user_name,
            "nickname": u.nickname,
            "status": u.status,
        },
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "id": str(u.user_id),
                "user_name": u.user_name,
                "nickname": u.nickname or "",
                "user_phone": u.user_phone or "",
                "user_email": u.user_email or "",
                "user_gender": u.user_gender or "0",
                "status": u.status,
            },
            audit={"user_id": str(u.user_id), "user_name": u.user_name},
            label_key="ai.tool.user.lookup.result",
            label_params={"userName": u.user_name},
        ),
    )


# spec §10 Task 25：user.update — 字段级更新（白名单控制）
# spec §2.21 OVERWRITE_ALLOWED：nickname / user_email / user_phone / user_gender /
# status / dept_id / role_ids（不含 user_name / hashed_password / user_id）
_USER_UPDATE_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "nickname",
        "user_email",
        "user_phone",
        "user_gender",
        "status",
    }
)
"""user.update tool 允许更新的字段白名单（spec §2.21 OVERWRITE_ALLOWED 子集）。

不含 user_name / user_id / hashed_password（不可改）；
不含 dept_id / role_ids（需独立 tool user.update_dept / user.update_roles，预留 Task 25a+）。
"""


@ai_tool(
    AiToolMeta(
        name="user.update",
        agent="user_mgmt",
        summary=(
            "Update user profile (nickname/phone/email/gender/status) "
            "→ {'updated': 1}. HITL confirms."
        ),
        required_perms=("system:user:edit",),
        risk="high",
        hitl_always=True,
        dry_run_supported=True,
        result_view="rows_affected",
        args_summary_fields=("user_id",),
    )
)
async def user_update(
    ctx: AiToolContext,
    *,
    user_id: int,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
    user_gender: str | None = None,
    status: str | None = None,
) -> ToolResult:
    """更新用户资料（spec §10 Task 25）

    HITL 强制（hitl_always=True）：用户必须在抽屉确认后才真正落库。
    所有字段可选，仅传需更新的字段（PATCH 语义）。

    字段白名单：nickname / user_email / user_phone / user_gender / status
    （不含 user_name / hashed_password / user_id，详见 _USER_UPDATE_ALLOWED_FIELDS）。

    Args:
        user_id: Snowflake int64 ID（必填，更新锚点）
        nickname: 新昵称（2-16 字符）
        user_email: 新邮箱（合法 email）
        user_phone: 新手机号（合法手机号）
        user_gender: '0' (未知) / '1' (男) / '2' (女)
        status: '1' (启用) / '2' (禁用)
    """
    from app.core.exceptions import (  # noqa: PLC0415
        BusinessRuleException,
        NotFoundException,
    )

    # 至少一个可更新字段
    update_payload = {
        "nickname": nickname,
        "user_email": user_email,
        "user_phone": user_phone,
        "user_gender": user_gender,
        "status": status,
    }
    provided = {k: v for k, v in update_payload.items() if v is not None}
    if not provided:
        raise BusinessRuleException(
            "至少提供一个待更新字段（nickname / user_email / user_phone / user_gender / status）",
            error_code="AI_USER_UPDATE_NO_FIELDS",
        )

    # spec §6.2 data_scope 强制：写 tool 含 user_id 必须先验证 target 可见
    await ensure_targets_in_scope(ctx, user_ids=[user_id])

    # 查询用户（应用 data_scope 二次防御）
    stmt = select(User).where(User.user_id == user_id, *ctx.data_scope.filters)
    user = (await ctx.db.execute(stmt)).scalars().first()
    if not user:
        raise NotFoundException("用户", error_code="AI_USER_UPDATE_NOT_FOUND")

    # 字段白名单已通过函数签名保证（只接受 _USER_UPDATE_ALLOWED_FIELDS 内的字段）
    for field, value in provided.items():
        if field not in _USER_UPDATE_ALLOWED_FIELDS:
            # defensive：函数签名不该接受，但兜底防 LLM 注入
            raise BusinessRuleException(
                f"字段 {field} 不在可更新白名单内",
                error_code="AI_USER_UPDATE_FIELD_NOT_ALLOWED",
            )
        setattr(user, field, value)

    return ToolResult.success(
        data={"updated": 1, "userName": user.user_name},
        ui=UIResult(
            view_type="rows_affected",
            view_data={"count": 1, "ids": [str(user.user_id)]},
            audit={
                "affected_user_ids": [str(user.user_id)],
                "fields": list(provided.keys()),
            },
            label_key="ai.tool.user.update.result",
            label_params={"userName": user.user_name},
        ),
    )


async def _dry_run_user_update(
    ctx: AiToolContext,
    *,
    user_id: int,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
    user_gender: str | None = None,
    status: str | None = None,
) -> Any:
    """dry_run：列出待更新字段 + 用户当前值供 HITL 抽屉确认（spec §10 Task 25）"""
    from app.core.exceptions import (  # noqa: PLC0415
        AuthorizationException,
    )
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    update_payload = {
        "nickname": nickname,
        "user_email": user_email,
        "user_phone": user_phone,
        "user_gender": user_gender,
        "status": status,
    }
    provided = {k: v for k, v in update_payload.items() if v is not None}
    if not provided:
        return DryRunResult(
            ok=False,
            count=0,
            reason="至少提供一个待更新字段",
        )

    try:
        await ensure_targets_in_scope(ctx, user_ids=[user_id])
    except AuthorizationException as e:
        return DryRunResult(ok=False, count=0, reason=e.message)

    stmt = select(User).where(User.user_id == user_id, *ctx.data_scope.filters)
    user = (await ctx.db.execute(stmt)).scalars().first()
    if not user:
        return DryRunResult(
            ok=False,
            count=0,
            reason="用户不存在 / 不在可见范围",
        )

    examples = [
        f"{field}: {getattr(user, field)} → {value}"
        for field, value in provided.items()
    ]
    summary = f"将更新用户 {user.user_name} 的 {len(provided)} 个字段"
    return DryRunResult(
        ok=True,
        count=1,
        reason=summary,
        examples=examples,
    )


# ============ user.import_preview / user.import_execute（spec §10 Task 26/26a） ============


async def _load_file_bytes(ctx: AiToolContext, file_id: str) -> tuple[bytes, str, str]:
    """从 sys_file 加载 file_bytes + filename + mime_type（spec §10 Task 26）

    抛 BusinessRuleException:
        - AI_FILE_ID_INVALID: file_id 不是合法数字字符串
        - AI_FILE_NOT_FOUND: file_id 不存在 / 已删除

    Returns: (file_bytes, filename, mime_type)
    """
    from pathlib import Path  # noqa: PLC0415

    from app.core.exceptions import BusinessRuleException  # noqa: PLC0415
    from app.modules.system.models.file import File  # noqa: PLC0415

    try:
        file_id_int = int(file_id)
    except (TypeError, ValueError) as e:
        raise BusinessRuleException(
            f"file_id 格式无效: {file_id!r}",
            error_code="AI_FILE_ID_INVALID",
        ) from e

    stmt = select(File).where(File.file_id == file_id_int, File.del_flag == "0")
    file_record = (await ctx.db.execute(stmt)).scalars().first()
    if file_record is None:
        raise BusinessRuleException(
            f"文件不存在: file_id={file_id}",
            error_code="AI_FILE_NOT_FOUND",
        )

    file_path = Path(file_record.file_path)
    if not file_path.exists():  # noqa: ASYNC240  AI 调用频次低，同步 IO 可接受
        raise BusinessRuleException(
            f"文件已被删除（DB 记录存在但磁盘文件丢失）: file_id={file_id}",
            error_code="AI_FILE_NOT_FOUND",
        )

    file_bytes = file_path.read_bytes()  # noqa: ASYNC240
    return file_bytes, file_record.file_name, file_record.mime_type or ""


@ai_tool(
    AiToolMeta(
        name="user.import_preview",
        agent="user_mgmt",
        summary=(
            "Dry-run user import → {batchId, previewToken, summary}. "
            "Read-only. Call user.import_execute next."
        ),
        required_perms=("system:user:import",),
        risk="low",
        readonly=True,
        accepts_file=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "text/csv",
        ),
        result_view="detail_card",
        chip_target="/system/user",
        args_summary_fields=("file_id", "reason"),
    )
)
async def user_import_preview(
    ctx: AiToolContext,
    *,
    file_id: str,
    reason: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
) -> ToolResult:
    """AI tool 入口：解析 + dry_run 用户导入（spec §10 Task 26, v2.2 P0 #2.14 拆分）

    流程（spec §6 / §2.14）：
    1. _load_file_bytes(file_id) → file_bytes + filename + mime_type
    2. user_service.parse_import_excel(file_bytes, mime_type) → records
    3. dry_run_import_users(records, current_user, file_bytes, filename, reason,
                             on_conflict=...) → (ImportDryRunResult, batch)
    4. ToolResult.success(
         data={batch_id, preview_token, summary{new, exists, conflict, out_of_scope}},
         ui=detail_card（HITL 抽屉展示 summary 供用户确认）
       )

    **强制 HITL**：本 tool 是 readonly，不写用户；execute 走 user.import_execute
    （risk=high + hitl_always），LLM 不能跳过 preview 直接 execute。

    Args:
        file_id: 文件 ID（sys_file.file_id 字符串形式）
        reason: 业务理由（1-256 字符，spec §2.30 P1-3 强制）
        on_conflict: 'skip' (默认) / 'overwrite' / 'fail_fast'（详见 spec §2.7）
    """
    from app.modules.system.service.user_service import user_service  # noqa: PLC0415
    from app.modules.system.user.import_service import (  # noqa: PLC0415
        dry_run_import_users,
    )

    file_bytes, filename, mime_type = await _load_file_bytes(ctx, file_id)
    records = user_service.parse_import_excel(file_bytes, mime_type)

    dry_run_result, batch = await dry_run_import_users(
        ctx.db,
        records,
        ctx.user,
        file_bytes,
        filename,
        reason,
        on_conflict=on_conflict,
    )

    summary = {
        "new": dry_run_result.new_count,
        "exists": dry_run_result.exists_count,
        "conflict": dry_run_result.conflict_count,
        "outOfScope": dry_run_result.out_of_scope_count,
    }
    return ToolResult.success(
        data={
            "batchId": batch.batch_id,
            "previewToken": batch.preview_token,
            "total": dry_run_result.total,
            "summary": summary,
        },
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "batchId": batch.batch_id,
                "previewToken": batch.preview_token[:8],  # 截断展示前 8 位
                "total": dry_run_result.total,
                "summary": summary,
                "expiresAt": (
                    batch.created_at.isoformat() if batch.created_at else None
                ),
            },
            audit={
                "batch_id": batch.batch_id,
                "total_rows": dry_run_result.total,
            },
            label_key="ai.tool.user.import_preview.result",
            label_params={"total": dry_run_result.total},
        ),
    )


@ai_tool(
    AiToolMeta(
        name="user.import_execute",
        agent="user_mgmt",
        summary=(
            "Execute previewed user import → {successCount}. "
            "HITL confirms. Pass previewToken from preview."
        ),
        required_perms=("system:user:import",),
        risk="high",
        hitl_always=True,
        dry_run_supported=True,
        result_view="rows_affected",
        args_summary_fields=("preview_token", "reason"),
    )
)
async def user_import_execute(
    ctx: AiToolContext,
    *,
    preview_token: str,
    reason: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
    sync_mode: Literal["CREATE_ONLY", "UPDATE_PROFILE", "FULL_SYNC"] = "CREATE_ONLY",
) -> ToolResult:
    """AI tool 入口：执行用户导入（spec §10 Task 26a, v2.2 P0 #2.14 拆分）

    **强制 HITL**（hitl_always=True）：用户必须在抽屉确认 preview summary 后才执行。
    LLM 不能跳过 preview → 直接 execute（spec §2.14 反例）。

    流程：
    1. 凭 preview_token 反查 batch（含 file_sha256 + records_hash + operator_id）
    2. 从 sys_file 重新加载 file_bytes（spec §2.19 file_storage_key 在 execute 阶段读）
    3. parse_import_excel → records（与 preview 时 hash 一致）
    4. batch_create_users_from_records(...) → ImportResult
    5. ToolResult.rows_affected（successCount + skippedCount + ...）

    Args:
        preview_token: 来自 user.import_preview 返回值，10min TTL
        reason: 业务理由（必须与 preview 时一致，spec §2.30）
        on_conflict: 与 preview 时一致（spec §2.7）
        sync_mode: 'CREATE_ONLY' (默认) / 'UPDATE_PROFILE' / 'FULL_SYNC'（spec §2.24）
    """
    from app.modules.system.service.user_service import user_service  # noqa: PLC0415
    from app.modules.system.user.constants import EmployeeNoSyncMode  # noqa: PLC0415
    from app.modules.system.user.import_service import (  # noqa: PLC0415  # noqa: PLC0415
        batch_create_users_from_records,
        get_batch_by_preview_token,
    )

    # 1. 反查 batch 拿 file 信息
    batch = await get_batch_by_preview_token(ctx.db, preview_token)
    if batch is None:
        from app.core.exceptions import UnprocessableEntityException  # noqa: PLC0415

        raise UnprocessableEntityException(
            "preview_token 无效或已过期",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    # 2. 直接从 batch.file_storage_key 读 file_bytes（spec §2.19 line 575 设计）
    #    dry_run 阶段已把上传文件保存到 LocalFileStorage（或 Phase 3 S3）；
    #    execute 阶段凭 batch.file_storage_key 反查，不需要 LLM 重新传 file_id。
    from pathlib import Path  # noqa: PLC0415

    if not batch.file_storage_key:
        from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

        raise BusinessRuleException(
            "批次未关联上传文件，无法 execute（请重新 import_preview）",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    file_path = Path(batch.file_storage_key)
    if not file_path.exists():  # noqa: ASYNC240  AI 调用频次低，同步 IO 可接受
        from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

        raise BusinessRuleException(
            f"预检文件已丢失（{batch.filename}），请重新 import_preview",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    file_bytes = file_path.read_bytes()  # noqa: ASYNC240
    filename = batch.filename or ""

    # 3. parse + execute
    records = user_service.parse_import_excel(
        file_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    result = await batch_create_users_from_records(
        ctx.db,
        records,
        preview_token=preview_token,
        file_bytes=file_bytes,
        filename=filename,
        reason=reason,
        current_user=ctx.user,
        on_conflict=on_conflict,
        sync_mode=EmployeeNoSyncMode(sync_mode),
    )

    return ToolResult.success(
        data={
            "successCount": result.success_count,
            "skippedCount": result.skipped_count,
            "overwrittenCount": result.overwritten_count,
            "failedCount": result.failed_count,
            "batchId": result.batch_id,
        },
        ui=UIResult(
            view_type="rows_affected",
            view_data={
                "count": result.success_count,
                "ids": [result.batch_id],  # batch_id 而非 user_ids（避免大量 ID 进 UI）
            },
            audit={
                "batch_id": result.batch_id,
                "success_count": result.success_count,
                "failed_count": result.failed_count,
            },
            label_key="ai.tool.user.import_execute.result",
            label_params={"count": result.success_count},
        ),
    )


async def _dry_run_user_import_execute(
    ctx: AiToolContext,
    *,
    preview_token: str,
    reason: str,  # noqa: ARG001  与 execute 签名对齐，dry_run 阶段不校验 reason（execute 入口校验）
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
    sync_mode: Literal["CREATE_ONLY", "UPDATE_PROFILE", "FULL_SYNC"] = "CREATE_ONLY",
) -> Any:
    """dry_run：列出 batch summary 供 HITL 抽屉二次确认（spec §10 Task 26a）

    本 tool 已 hitl_always=True 强制走 HITL；dry_run 是「确认前再看一次 summary」，
    返回 batch_id + total + 四象限计数 + 文件名（让用户对照确认）。
    """
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415
    from app.modules.system.user.import_service import (  # noqa: PLC0415
        get_batch_by_preview_token,
    )

    batch = await get_batch_by_preview_token(ctx.db, preview_token)
    if batch is None:
        return DryRunResult(
            ok=False,
            count=0,
            reason="preview_token 无效或已过期",
        )

    summary = (
        f"将执行批次 {batch.batch_id[:8]}… "
        f"共 {batch.total_rows} 行 "
        f"(新增 {batch.summary_new} / 已存在 {batch.summary_exists} / "
        f"冲突 {batch.summary_conflict} / 越界 {batch.summary_out_of_scope})"
    )
    examples = [
        f"批次 ID: {batch.batch_id}",
        f"文件名: {batch.filename or '(未知)'}",
        f"冲突策略: {on_conflict}",
        f"同步模式: {sync_mode}",
    ]
    return DryRunResult(
        ok=True,
        count=batch.summary_new,  # 预计将新增的用户数
        reason=summary,
        examples=examples,
    )


# ============ user.export（spec §10 Task 27） ============


@ai_tool(
    AiToolMeta(
        name="user.export",
        agent="user_mgmt",
        summary=(
            "Export users to xlsx → {exportId, rowCount}. "
            "Reason required. Filter via name/email/status."
        ),
        required_perms=("system:user:export",),
        risk="high",
        readonly=False,  # 写 ExportTask 表 + 生成 xlsx 文件
        produces_file=True,
        dry_run_supported=True,
        result_view="rows_affected",
        args_summary_fields=("reason",),
    )
)
async def user_export(
    ctx: AiToolContext,
    *,
    reason: str,
    user_name: str | None = None,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
    status: Literal["0", "1"] | None = None,
) -> ToolResult:
    """AI tool 入口：导出用户列表到 Excel（spec §10 Task 27）

    强制建 ExportTask（spec §2.31 P1-5）+ filter_snapshot 冻结 + 30 天 TTL。
    行数 > USER_EXPORT_ASYNC_THRESHOLD（5000）抛 AI_EXPORT_ASYNC_REQUIRED，
    Phase 3 异步通道上线后改为自动入队。

    Args:
        reason: 业务理由（必填，spec §2.30 P1-3，1-256 字符）
        user_name / nickname / user_email / user_phone: filter（可选）
        status: '1' (启用) / '0' (禁用)，None=不过滤
    """
    from app.modules.system.user.export_service import (  # noqa: PLC0415
        export_users_to_excel,
    )
    from app.modules.system.user.schemas import UserExportFilter  # noqa: PLC0415

    filter_ = UserExportFilter(
        user_name=user_name,
        nickname=nickname,
        user_email=user_email,
        user_phone=user_phone,
        status=status,
    )

    _xlsx_bytes, row_count, export_id = await export_users_to_excel(
        ctx.db,
        filter_,
        ctx.user,
        reason=reason,
    )

    return ToolResult.success(
        data={
            "exportId": export_id,
            "rowCount": row_count,
        },
        ui=UIResult(
            view_type="rows_affected",
            view_data={
                "count": row_count,
                "export_id": export_id,
            },
            audit={
                "export_id": export_id,
                "row_count": row_count,
                "filter": {
                    "user_name": user_name,
                    "nickname": nickname,
                    "user_email": user_email,
                    "user_phone": user_phone,
                    "status": status,
                },
            },
            label_key="ai.tool.user.export.result",
            label_params={"count": row_count},
        ),
    )


async def _dry_run_user_export(
    ctx: AiToolContext,
    *,
    reason: str,  # noqa: ARG001  与 execute 签名对齐；dry_run 阶段不重复校验 reason
    user_name: str | None = None,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
    status: Literal["0", "1"] | None = None,
) -> Any:
    """dry_run：预估导出行数供 HITL 抽屉确认（spec §10 Task 27）

    用 User.count(*) + filter 估算行数，不实际跑导出（避免重复建 task）。
    行数 > USER_EXPORT_ASYNC_THRESHOLD → 提示用户缩窄 filter；行数为 0 → 警告。
    """
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    base = select(User).where(*ctx.data_scope.filters)
    if user_name:
        base = base.where(User.user_name.ilike(f"%{user_name}%"))
    if nickname:
        base = base.where(User.nickname.ilike(f"%{nickname}%"))
    if user_email:
        base = base.where(User.user_email == user_email)
    if user_phone:
        base = base.where(User.user_phone == user_phone)
    if status is not None:
        base = base.where(User.status == status)

    estimated = int(
        await ctx.db.scalar(select(func.count()).select_from(base.subquery())) or 0
    )

    if estimated == 0:
        return DryRunResult(
            ok=False,
            count=0,
            reason="筛选条件下无用户匹配，导出会生成空文件",
        )

    from app.modules.system.user.constants import (  # noqa: PLC0415
        USER_EXPORT_ASYNC_THRESHOLD,
    )

    if estimated > USER_EXPORT_ASYNC_THRESHOLD:
        return DryRunResult(
            ok=False,
            count=estimated,
            reason=(
                f"预计导出 {estimated} 行，超过同步阈值 {USER_EXPORT_ASYNC_THRESHOLD}，"
                "请缩窄 filter 或等待异步通道开放"
            ),
        )

    return DryRunResult(
        ok=True,
        count=estimated,
        reason=f"将导出约 {estimated} 行用户数据到 xlsx 文件（30 天后过期清理）",
        examples=[
            f"filter: user_name={user_name or '*'}, status={status or '*'}",
        ],
    )
