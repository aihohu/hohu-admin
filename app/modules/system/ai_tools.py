"""system 模块的 AI tool。

三个聚合 tool：
  user.count    → 返回 {"count": N}，用于"有多少"类问题
  user.stats    → 返回 [{"group": ..., "count": ...}]，用于按维度分布
  user.distinct → 返回 ["v1", "v2"]，用于枚举字段取值

用户聚合仅开放 status / user_gender 两个低基数字段，避免任意字段查询和敏感信息枚举。

注意：本模块 @ai_tool 装饰器执行期会把 tool 注册到 ToolRegistry，
启动时 ToolRegistry.validate_on_startup(db) 会校验 ai_agent 表里有
user_mgmt Agent 和工具声明的权限码。
"""

from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.core.exceptions import AuthorizationException
from app.modules.ai.agents.gateway import ensure_targets_in_scope
from app.modules.ai.agents.gateway.result import (
    PreparedActionProposal,
    ResultProjection,
    ToolResult,
    UIResult,
)
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.file_access import (
    IMPORT_MIME_TYPES_BY_EXTENSION,
    FileAccessPolicy,
    load_protected_file,
)
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


def _result_projection(
    subject_type: str | None = None,
    subject_ids: list[Any] | tuple[Any, ...] = (),
    *,
    scope_bound: bool = False,
) -> ResultProjection:
    refs = (
        tuple({"type": subject_type, "id": str(value)} for value in subject_ids)
        if subject_type is not None
        else ()
    )
    return ResultProjection(subject_refs=refs, scope_bound=scope_bound)


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
        status: '1' (启用) / '0' (禁用)
        user_gender: '0' (未知) / '1' (男) / '2' (女)

    注意：LLM 经常以 JSON int 传值（filters={"status": 1}），sys_user 字段是
    varchar，asyncpg 严格类型检查会抛 ProgrammingError。这里强制 stringify。
    """
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

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
    filters = validate_filters_in_whitelist(ctx.tool_meta, filters)

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
        projection=_result_projection(scope_bound=True),
        ui=UIResult(
            view_type="plain_json",
            view_data={"count": count},
            audit={"count": count},
            label_key="ai.tool.dept.count.result",
            label_params={"count": count},
        ),
    )


# ============ role.list / dept.list ============

# 只返回识别和展示所需字段，避免敏感信息进入模型上下文。
_LIST_MAX_LIMIT = 50  # 限制数据库结果和模型上下文大小。
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
        idempotent=True,
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
        {"key": "name", "label": "ai.tool.field.name"},
        {"key": "code", "label": "ai.tool.field.code"},
        {"key": "status", "label": "ai.tool.field.status"},
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
        projection=_result_projection(scope_bound=True),
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
        idempotent=True,
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
        {"key": "name", "label": "ai.tool.field.name"},
        {"key": "parent_id", "label": "ai.tool.field.parentDeptId"},
        {"key": "status", "label": "ai.tool.field.status"},
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
        projection=_result_projection(scope_bound=True),
        ui=UIResult(
            view_type="data_list",
            view_data={"columns": columns, "rows": records},
            audit={"total": total},
            label_key="ai.tool.dept.list.result",
            label_params={"count": total},
        ),
    )


# ============ user.dept_lookup / user.create / user.reset_password（2026-08-11 纠偏） ============

_DEPT_LOOKUP_MAX_MATCHES = 20


@ai_tool(
    AiToolMeta(
        name="user.dept_lookup",
        agent="user_mgmt",
        summary=(
            "Resolve an exact visible department name before user.create; "
            "returns candidate IDs and parents."
        ),
        required_perms=("system:user:add",),
        risk="low",
        readonly=True,
        idempotent=True,
        result_view="data_list",
        args_summary_fields=("dept_name",),
    )
)
async def user_dept_lookup(
    ctx: AiToolContext,
    *,
    dept_name: str,
) -> ToolResult:
    """按完整名称解析 user.create 可使用的可见、启用部门候选。"""
    from app.constants import STATUS_ENABLED  # noqa: PLC0415
    from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

    query = dept_name.strip()
    if not query:
        raise BusinessRuleException(
            "部门名称不能为空",
            error_code="AI_USER_DEPT_NAME_REQUIRED",
        )

    parent = aliased(Dept)
    filters = [
        Dept.dept_name == query,
        Dept.status == STATUS_ENABLED,
    ]
    if ctx.data_scope.accessible_dept_ids is not None:
        filters.append(Dept.dept_id.in_(ctx.data_scope.accessible_dept_ids))

    match_count = int(
        await ctx.db.scalar(select(func.count(Dept.dept_id)).where(*filters)) or 0
    )
    rows = (
        await ctx.db.execute(
            select(Dept, parent.dept_name.label("parent_name"))
            .outerjoin(parent, Dept.parent_id == parent.dept_id)
            .where(*filters)
            .order_by(Dept.parent_id.asc().nulls_first(), Dept.dept_id.asc())
            .limit(_DEPT_LOOKUP_MAX_MATCHES)
        )
    ).all()

    matches = [
        {
            "id": str(dept.dept_id),
            "name": dept.dept_name,
            "parentId": str(dept.parent_id) if dept.parent_id else None,
            "parentName": parent_name,
        }
        for dept, parent_name in rows
    ]
    return ToolResult.success(
        data={
            "query": query,
            "matchCount": match_count,
            "matches": matches,
        },
        projection=_result_projection("dept", [match["id"] for match in matches]),
        ui=UIResult(
            view_type="data_list",
            view_data={
                "columns": [
                    {"key": "id", "label": "ID"},
                    {"key": "name", "label": "page.system.dept.deptName"},
                    {"key": "parentName", "label": "page.system.dept.parentId"},
                ],
                "rows": matches,
            },
            audit={
                "query": query,
                "match_count": match_count,
                "returned_count": len(matches),
            },
            label_key="ai.tool.user.dept_lookup.result",
            label_params={"count": match_count},
        ),
    )


async def _get_ai_default_password(ctx: AiToolContext) -> str:
    """读取并校验仅后端可见的默认密码策略。"""
    from app.core.exceptions import BusinessRuleException  # noqa: PLC0415
    from app.modules.system.user.helpers import (  # noqa: PLC0415
        get_default_password,
    )
    from app.utils.validators import validate_password  # noqa: PLC0415

    try:
        default_password = await get_default_password(ctx.db)
    except BusinessRuleException as exc:
        if exc.error_code == "AI_IMPORT_DEFAULT_PASSWORD_INVALID":
            raise BusinessRuleException(
                "生产环境默认密码未显式配置，无法执行用户凭据操作",
                error_code="AI_USER_DEFAULT_PASSWORD_INVALID",
            ) from exc
        raise BusinessRuleException(
            "系统默认密码未配置，无法执行用户凭据操作",
            error_code="AI_USER_DEFAULT_PASSWORD_NOT_SET",
        ) from exc

    try:
        validate_password(default_password)
    except ValueError:
        raise BusinessRuleException(
            "系统默认密码不符合强度要求，请先更新 auth:default_password",
            error_code="AI_USER_DEFAULT_PASSWORD_INVALID",
        ) from None
    return default_password


async def _load_ai_create_policy(
    ctx: AiToolContext,
    *,
    primary_dept_id: int,
) -> tuple[Dept, Role, str]:
    """校验新用户的部门、默认角色与默认密码策略。"""
    from app.constants import STATUS_ENABLED, USER_ROLE_CODE  # noqa: PLC0415
    from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

    dept = await ctx.db.scalar(
        select(Dept).where(
            Dept.dept_id == primary_dept_id,
            Dept.status == STATUS_ENABLED,
        )
    )
    if dept is None:
        raise BusinessRuleException(
            "主部门不存在或已禁用",
            error_code="AI_USER_PRIMARY_DEPT_NOT_FOUND",
        )

    role = await ctx.db.scalar(
        select(Role).where(
            Role.role_code == USER_ROLE_CODE,
            Role.status == STATUS_ENABLED,
        )
    )
    if role is None:
        raise BusinessRuleException(
            f"默认角色 {USER_ROLE_CODE} 不存在或已禁用",
            error_code="AI_USER_DEFAULT_ROLE_NOT_FOUND",
        )

    return dept, role, await _get_ai_default_password(ctx)


def _build_ai_user_create_schema(
    *,
    user_name: str,
    nickname: str | None,
    user_email: str | None,
    user_phone: str | None,
    user_gender: str | None,
    status: str,
    primary_dept_id: int,
    default_password: str,
) -> Any:
    """用 HTTP 同款 schema 校验 AI 创建参数，不回显敏感值。"""
    from pydantic import ValidationError  # noqa: PLC0415

    from app.core.exceptions import BusinessRuleException  # noqa: PLC0415
    from app.modules.system.schemas.user import (  # noqa: PLC0415
        UserCreate,
        UserDeptItem,
    )

    try:
        return UserCreate(
            user_name=user_name,
            nickname=nickname,
            user_email=user_email,
            user_phone=user_phone,
            user_gender=user_gender,
            status=status,
            dept_ids=[UserDeptItem(dept_id=str(primary_dept_id), is_primary=True)],
            password=default_password,
        )
    except ValidationError:
        # Pydantic 的错误上下文可能含 input；不能把 exc 文本带进 LLM。
        raise BusinessRuleException(
            "用户资料格式不合法，请检查用户名、昵称、邮箱、手机号、性别和状态",
            error_code="AI_USER_CREATE_INVALID",
        ) from None


@ai_tool(
    AiToolMeta(
        name="user.create",
        agent="user_mgmt",
        summary="Create one user in a primary dept with backend password/default role; HITL confirms.",
        required_perms=("system:user:add",),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        sensitive_input=("password", "initial_role_ids"),
        sensitive_output=("hashed_password",),
        result_view="detail_card",
        args_summary_fields=("user_name", "primary_dept_id"),
    )
)
async def user_create(
    ctx: AiToolContext,
    *,
    user_name: str,
    primary_dept_id: int,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
    user_gender: str | None = "0",
    status: str = "1",
) -> ToolResult:
    """创建单个用户；密码与角色完全由后端策略决定。"""
    from app.constants import USER_ROLE_CODE  # noqa: PLC0415
    from app.modules.system.service.dept_service import (  # noqa: PLC0415
        dept_service,
    )
    from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
        user_role_assignment_service,
    )
    from app.modules.system.service.user_service import (  # noqa: PLC0415
        user_service,
    )

    # 创建目标部门必须在调用者的数据权限范围内。
    await ensure_targets_in_scope(ctx, dept_ids=[primary_dept_id])
    dept, role, default_password = await _load_ai_create_policy(
        ctx,
        primary_dept_id=primary_dept_id,
    )
    user_in = _build_ai_user_create_schema(
        user_name=user_name,
        nickname=nickname,
        user_email=user_email,
        user_phone=user_phone,
        user_gender=user_gender,
        status=status,
        primary_dept_id=primary_dept_id,
        default_password=default_password,
    )

    new_user = await user_service.create_user(ctx.db, user_in)
    await ctx.db.flush()
    await user_role_assignment_service.assign_created_user_roles(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        target_user_id=new_user.user_id,
        role_ids=None,
        dept_ids=[primary_dept_id],
    )
    await dept_service.update_user_depts(
        ctx.db,
        new_user.user_id,
        [{"dept_id": primary_dept_id, "is_primary": True}],
    )
    await ctx.db.flush()

    user_id = str(new_user.user_id)
    dept_id = str(primary_dept_id)
    return ToolResult.success(
        data={
            "created": 1,
            "userId": user_id,
            "userName": new_user.user_name,
            "roleCode": USER_ROLE_CODE,
            "primaryDeptId": dept_id,
            "passwordPolicy": "system_default",
        },
        projection=ResultProjection(
            subject_refs=(
                {"type": "user", "id": user_id},
                {"type": "dept", "id": dept_id},
            )
        ),
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "title": new_user.user_name,
                "fields": [
                    {
                        "label": "page.system.user.userName",
                        "value": new_user.user_name,
                    },
                    {
                        "label": "page.system.user.primaryDept",
                        "value": dept.dept_name,
                    },
                    {
                        "label": "page.system.user.userRole",
                        "value": role.role_code,
                    },
                    {
                        "label": "page.system.user.userStatus",
                        "value": new_user.status,
                    },
                ],
            },
            audit={
                "affected_user_ids": [user_id],
                "role_code": role.role_code,
                "primary_dept_id": dept_id,
                "password_policy": "system_default",
            },
            label_key="ai.tool.user.create.result",
            label_params={"userName": new_user.user_name},
        ),
    )


async def _dry_run_user_create(
    ctx: AiToolContext,
    *,
    user_name: str,
    primary_dept_id: int,
    nickname: str | None = None,
    user_email: str | None = None,
    user_phone: str | None = None,
    user_gender: str | None = "0",
    status: str = "1",
) -> Any:
    """预检创建目标、唯一性与后端默认策略，不写业务数据。"""
    from app.core.exceptions import BusinessException  # noqa: PLC0415
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    try:
        await ensure_targets_in_scope(ctx, dept_ids=[primary_dept_id])
        dept, role, default_password = await _load_ai_create_policy(
            ctx,
            primary_dept_id=primary_dept_id,
        )
        _build_ai_user_create_schema(
            user_name=user_name,
            nickname=nickname,
            user_email=user_email,
            user_phone=user_phone,
            user_gender=user_gender,
            status=status,
            primary_dept_id=primary_dept_id,
            default_password=default_password,
        )
    except BusinessException as exc:
        return DryRunResult(ok=False, count=0, reason=exc.message)

    exists = await ctx.db.scalar(
        select(func.count(User.user_id)).where(User.user_name == user_name)
    )
    if exists:
        return DryRunResult(
            ok=False,
            count=0,
            reason="用户名已存在，请更换账号",
            confirmation_fields=[
                {
                    "label": "primary_dept_id",
                    "value": primary_dept_id,
                    "display_value": f"{dept.dept_name}（{primary_dept_id}）",
                }
            ],
        )

    return DryRunResult(
        ok=True,
        count=1,
        reason=f"将创建用户 {user_name} 并加入主部门 {dept.dept_name}",
        examples=[
            f"账号：{user_name}",
            f"主部门：{dept.dept_name}",
            f"默认角色：{role.role_code}",
            "密码策略：系统默认密码（不展示明文）",
        ],
        confirmation_fields=[
            {
                "label": "primary_dept_id",
                "value": primary_dept_id,
                "display_value": f"{dept.dept_name}（{primary_dept_id}）",
            }
        ],
    )


async def _load_ai_reset_target(ctx: AiToolContext, *, user_id: int) -> User:
    """读取重置目标并应用保底账号/当前账号保护。"""
    from app.constants import (  # noqa: PLC0415
        ADMIN_USERNAME,
        STATUS_ENABLED,
        SUPER_ADMIN_ROLE_CODE,
    )
    from app.core.exceptions import (  # noqa: PLC0415
        AuthorizationException,
        BusinessRuleException,
        NotFoundException,
    )
    from app.db.base import user_roles  # noqa: PLC0415

    target = await ctx.db.scalar(
        select(User).where(User.user_id == user_id, *ctx.data_scope.filters)
    )
    if target is None:
        raise NotFoundException(
            "用户",
            error_code="AI_USER_RESET_NOT_FOUND",
        )
    if target.user_id == ctx.user.user_id:
        raise BusinessRuleException(
            "不能通过 AI 重置当前登录账号，请使用个人改密功能",
            error_code="AI_USER_RESET_SELF_FORBIDDEN",
        )
    target_has_super_role = bool(
        await ctx.db.scalar(
            select(func.count())
            .select_from(user_roles.join(Role, user_roles.c.role_id == Role.role_id))
            .where(
                user_roles.c.user_id == target.user_id,
                Role.role_code == SUPER_ADMIN_ROLE_CODE,
                Role.status == STATUS_ENABLED,
            )
        )
    )
    if target.user_name == ADMIN_USERNAME or target_has_super_role:
        actor_is_super_admin = ctx.user.user_name == ADMIN_USERNAME
        if not actor_is_super_admin:
            actor_is_super_admin = bool(
                await ctx.db.scalar(
                    select(func.count())
                    .select_from(
                        user_roles.join(Role, user_roles.c.role_id == Role.role_id)
                    )
                    .where(
                        user_roles.c.user_id == ctx.user.user_id,
                        Role.role_code == SUPER_ADMIN_ROLE_CODE,
                        Role.status == STATUS_ENABLED,
                    )
                )
            )
        if not actor_is_super_admin:
            raise AuthorizationException(
                "只有超级管理员可以重置系统管理员密码",
                error_code="AI_SUPER_ADMIN_REQUIRED",
            )
    return target


@ai_tool(
    AiToolMeta(
        name="user.reset_password",
        agent="user_mgmt",
        summary="Reset one user's password to the backend default policy; HITL confirms.",
        required_perms=("system:user:reset-password",),
        risk="high",
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        sensitive_input=("password",),
        sensitive_output=("hashed_password",),
        result_view="rows_affected",
        args_summary_fields=("user_id",),
    )
)
async def user_reset_password(ctx: AiToolContext, *, user_id: int) -> ToolResult:
    """把目标用户密码重置为后端默认策略，绝不返回明文。"""
    from app.modules.system.schemas.user import ResetPassword  # noqa: PLC0415
    from app.modules.system.service.user_service import (  # noqa: PLC0415
        user_service,
    )

    await ensure_targets_in_scope(ctx, user_ids=[user_id])
    target = await _load_ai_reset_target(ctx, user_id=user_id)
    default_password = await _get_ai_default_password(ctx)
    await user_service.reset_password(
        ctx.db,
        user_id,
        ResetPassword(new_password=default_password),
    )

    str_user_id = str(user_id)
    return ToolResult.success(
        data={
            "updated": 1,
            "userId": str_user_id,
            "userName": target.user_name,
            "passwordPolicy": "system_default",
        },
        projection=_result_projection("user", [str_user_id]),
        ui=UIResult(
            view_type="rows_affected",
            view_data={"count": 1, "ids": [str_user_id]},
            audit={
                "affected_user_ids": [str_user_id],
                "password_policy": "system_default",
            },
            label_key="ai.tool.user.reset_password.result",
            label_params={"userName": target.user_name},
        ),
    )


async def _dry_run_user_reset_password(
    ctx: AiToolContext,
    *,
    user_id: int,
) -> Any:
    """预检重置目标与默认密码策略，不读取或展示密码值。"""
    from app.core.exceptions import BusinessException  # noqa: PLC0415
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    try:
        await ensure_targets_in_scope(ctx, user_ids=[user_id])
        target = await _load_ai_reset_target(ctx, user_id=user_id)
        await _get_ai_default_password(ctx)
    except BusinessException as exc:
        return DryRunResult(ok=False, count=0, reason=exc.message)

    return DryRunResult(
        ok=True,
        count=1,
        reason=f"将重置用户 {target.user_name} 的密码",
        examples=[
            f"目标账号：{target.user_name}",
            "影响：旧密码立即失效",
            "新密码策略：系统默认密码（不展示明文）",
        ],
        confirmation_fields=[
            {
                "label": "user_id",
                "value": user_id,
                "display_value": f"{target.user_name}（{user_id}）",
            }
        ],
    )


# ============ user.batch_delete ============


async def _resolve_users(
    ctx: AiToolContext,
    *,
    user_ids: list[int] | None,
    user_names: list[str] | None,
    phones: list[str] | None,
) -> list[User]:
    """按 IDs、用户名或手机号解析调用者可见的用户列表。

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
        readonly=False,
        idempotent=False,
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
    # Durable confirmation freezes exact IDs. Validate that exact set before
    # resolving rows; otherwise a later scope reduction would silently turn an
    # approved N-row delete into a smaller partial delete.
    if user_ids and user_names is None and phones is None:
        await ensure_targets_in_scope(ctx, user_ids=user_ids)

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
    # 写操作在解析后再次验证完整目标集合，防止权限变化导致部分执行。
    await ensure_targets_in_scope(ctx, user_ids=resolved_ids)

    from app.modules.system.service.user_service import user_service  # noqa: PLC0415

    count = await user_service.batch_delete_users(
        ctx.db, resolved_ids, current_user_id=ctx.user.user_id
    )
    # 模型只接收删除数量；用户 ID 仅进入结构化 UI 和审计数据。
    str_ids = [str(i) for i in resolved_ids]
    return ToolResult.success(
        data={"deleted": count},
        projection=_result_projection("user", str_ids),
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

    users = sorted(users, key=lambda user: user.user_id)
    resolved_ids = [u.user_id for u in users]
    try:
        # 预览同样校验数据权限，防止通过估算接口探测越权目标。
        await ensure_targets_in_scope(ctx, user_ids=resolved_ids)
    except AuthorizationException as e:
        return DryRunResult(ok=False, count=0, reason=e.message)

    from app.modules.system.service.user_service import user_service  # noqa: PLC0415

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
        summary_key="page.ai.chat.confirmBatchDeleteSummary",
        summary_params={
            "count": len(users),
            "users": (
                f"{', '.join(u.user_name for u in users[:3])}"
                f"{'...' if len(users) > 3 else ''}"
            ),
        },
        examples=examples,
        execution_args={
            "user_ids": resolved_ids,
            "user_names": None,
            "phones": None,
        },
        business_snapshot=user_service.build_batch_delete_identity_snapshot(users),
    )


# ============ user.list / user.lookup / user.update ============


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
        idempotent=True,
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
    """列出用户，返回前 N 条精简字段。

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

    # 读取工具同样受调用者数据权限约束。
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
        {"key": "user_name", "label": "ai.tool.field.userName"},
        {"key": "nickname", "label": "ai.tool.field.nickname"},
        {"key": "status", "label": "ai.tool.field.status"},
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
        projection=_result_projection(scope_bound=True),
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
        idempotent=True,
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
    """查询单个用户详情。

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

    # 显式 ID 查询也必须验证目标可见，防止绕过列表过滤读取敏感字段。
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
        # 多重匹配返回首项并提示模型向用户澄清。
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
        projection=_result_projection("user", [u.user_id]),
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


# user.update 仅允许字段级白名单更新：nickname / user_email / user_phone / user_gender /
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
"""user.update 工具允许更新的字段白名单。

不含 user_name / user_id / hashed_password（不可改）；
部门和角色必须由独立工具更新，避免一个操作跨越多个权限边界。
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
        readonly=False,
        idempotent=False,
        hitl_always=True,
        dry_run_supported=True,
        result_view="rows_affected",
        args_summary_fields=(
            "user_id",
            "nickname",
            "user_email",
            "user_phone",
            "user_gender",
            "status",
        ),
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
    """更新用户资料。

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

    # 写操作必须先验证目标处于调用者的数据权限范围。
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
        projection=_result_projection("user", [user.user_id]),
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
    """列出待更新字段和当前值，供确认界面展示。"""
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
        confirmation_fields=[
            {
                "label": "user_id",
                "value": user_id,
                "display_value": f"{user.user_name}（{user_id}）",
            },
            *[{"label": field, "value": value} for field, value in provided.items()],
        ],
    )


# ============ user.import_preview / user.import_execute ============


_USER_IMPORT_FILE_POLICY = FileAccessPolicy(
    allowed_business_types=frozenset({"user-import"}),
    mime_types_by_extension=IMPORT_MIME_TYPES_BY_EXTENSION,
    max_bytes=10 * 1024 * 1024,
)


def _user_import_suffix_for_mime(mime_type: str) -> str:
    normalized = mime_type.split(";", maxsplit=1)[0].strip().lower()
    for suffix, allowed_mime_types in IMPORT_MIME_TYPES_BY_EXTENSION.items():
        if normalized in allowed_mime_types:
            return suffix

    from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

    raise BusinessRuleException(
        "导入文件类型不允许",
        error_code="AI_FILE_TYPE_NOT_ALLOWED",
    )


def _user_import_mime_for_filename(filename: str) -> str:
    suffix = (
        f".{filename.rsplit('.', maxsplit=1)[-1].lower()}" if "." in filename else ""
    )
    allowed_mime_types = IMPORT_MIME_TYPES_BY_EXTENSION.get(suffix)
    if allowed_mime_types:
        return next(iter(allowed_mime_types))

    from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

    raise BusinessRuleException(
        "预检文件类型无效，请重新 import_preview",
        error_code="AI_IMPORT_PREVIEW_INVALID",
    )


async def _load_file_bytes(ctx: AiToolContext, file_id: str) -> tuple[bytes, str, str]:
    """从受保护的 ``sys_file`` 加载用户导入文件。

    抛 BusinessRuleException:
        - AI_FILE_ID_INVALID: file_id 不是合法数字字符串
        - AI_FILE_NOT_FOUND: 不存在 / 已删除 / owner 或 tenant 不匹配
        - AI_FILE_TYPE_NOT_ALLOWED: 业务类型、扩展名、MIME 或 magic 不允许
        - AI_FILE_TOO_LARGE: DB 声明或磁盘实际大小超限
        - AI_FILE_PATH_INVALID: 路径越出私有上传根或不可安全读取

    Returns: (file_bytes, filename, mime_type)
    """
    protected = await load_protected_file(
        ctx,
        file_id,
        policy=_USER_IMPORT_FILE_POLICY,
    )
    # Use the resolved on-disk name: its suffix has already been checked against
    # the trusted DB extension and MIME.  ``record.file_name`` is a bare
    # Snowflake ID, so persisting it would lose the CSV/XLSX parser contract.
    return protected.data, protected.path.name, protected.mime_type


@ai_tool(
    AiToolMeta(
        name="user.import_preview",
        agent="user_mgmt",
        summary=(
            "Prepare user import; requested_outcome is required. Gateway owns "
            "confirmation and execution."
        ),
        required_perms=("system:user:import",),
        risk="low",
        readonly=False,
        idempotent=False,
        interaction_flow="prepared",
        prepared_execute_tool="user.import_execute",
        accepts_file=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/csv",
        ),
        result_view="detail_card",
        args_summary_fields=("file_id", "reason"),
    )
)
async def user_import_preview(
    ctx: AiToolContext,
    *,
    file_id: str,
    reason: str,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
    sync_mode: Literal["CREATE_ONLY", "UPDATE_PROFILE", "FULL_SYNC"] = "CREATE_ONLY",
) -> ToolResult:
    """解析用户导入文件并生成只读预览。

    流程：
    1. _load_file_bytes(file_id) → file_bytes + filename + mime_type
    2. parse_import_excel(file_bytes, mime_type) → records
    3. dry_run_import_users(records, current_user, file_bytes, filename, reason,
                             on_conflict=...) → (ImportDryRunResult, batch)
    4. ToolResult.success(
         data={batch_id, summary{new, exists, conflict, out_of_scope}},
         ui=detail_card（HITL 抽屉展示 summary 供用户确认）
       )

    **预检会写 artifact**：本 tool 不写 ``sys_user``，但每次都会新建 batch、
    cache 和预检文件，因此不是 readonly / idempotent。若模型请求执行，Gateway
    使用 PreparedActionProposal 自动进入绑定 execute 的 HITL，不再依赖第二次模型调用。

    Args:
        file_id: 文件 ID（sys_file.file_id 字符串形式）
        reason: 业务理由（1-256 字符）
        on_conflict: 'skip'（默认）/ 'overwrite' / 'fail_fast'
        sync_mode: 员工编号同步策略，在 preview 时冻结
    """
    from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
        user_role_assignment_service,
    )
    from app.modules.system.user.import_parser import (  # noqa: PLC0415
        import_file_has_column,
        parse_import_excel,
    )
    from app.modules.system.user.import_service import (  # noqa: PLC0415
        dry_run_import_users,
    )

    file_bytes, filename, mime_type = await _load_file_bytes(ctx, file_id)
    has_role_column = import_file_has_column(
        file_bytes,
        mime_type,
        "role_input",
    )
    await user_role_assignment_service.ensure_import_permissions(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        has_role_column=has_role_column,
    )
    records = parse_import_excel(file_bytes, mime_type)

    dry_run_result, batch = await dry_run_import_users(
        ctx.db,
        records,
        ctx.user,
        file_bytes,
        filename,
        reason,
        on_conflict=on_conflict,
        has_role_column=has_role_column,
    )

    # 预览阶段持久化文件；执行阶段按 storage key 读取，避免依赖客户端重复上传。
    from app.core.file_storage import get_file_storage  # noqa: PLC0415

    storage = get_file_storage()
    storage_suffix = _user_import_suffix_for_mime(mime_type)
    storage_key = await storage.save(
        file_bytes,
        mime_type=mime_type,
        namespace="import-preview",
        suffix=storage_suffix,
    )
    batch.file_storage_key = storage_key
    await ctx.db.flush()

    summary = {
        "new": dry_run_result.new_count,
        "exists": dry_run_result.exists_count,
        "conflict": dry_run_result.conflict_count,
        "outOfScope": dry_run_result.out_of_scope_count,
    }
    return ToolResult.success(
        data={
            "batchId": batch.batch_id,
            "total": dry_run_result.total,
            "summary": summary,
            "policy": {
                "onConflict": on_conflict,
                "syncMode": sync_mode,
            },
        },
        projection=_result_projection("user_import_batch", [batch.batch_id]),
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "batchId": batch.batch_id,
                "total": dry_run_result.total,
                "summary": summary,
                "policy": {
                    "onConflict": on_conflict,
                    "syncMode": sync_mode,
                },
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
        prepared_action=PreparedActionProposal(
            frozen_args={
                "preview_token": batch.preview_token,
                "reason": reason,
                "on_conflict": on_conflict,
                "sync_mode": sync_mode,
            },
            snapshot={
                "batch_id": str(batch.batch_id),
                "file_sha256": getattr(batch, "file_sha256", ""),
                "records_hash": getattr(batch, "records_hash", ""),
                "operator_id": getattr(batch, "operator_id", ctx.user.user_id),
                "total": dry_run_result.total,
                "summary": summary,
            },
            subject_ref={
                "type": "user_import_batch",
                "id": str(batch.batch_id),
            },
            presentation={
                "title": "确认导入用户",
                "fields": [
                    {"label": "total", "value": dry_run_result.total},
                    {"label": "new", "value": dry_run_result.new_count},
                    {"label": "exists", "value": dry_run_result.exists_count},
                    {"label": "conflict", "value": dry_run_result.conflict_count},
                    {
                        "label": "outOfScope",
                        "value": dry_run_result.out_of_scope_count,
                    },
                    {"label": "onConflict", "value": on_conflict},
                    {"label": "syncMode", "value": sync_mode},
                ],
                "warnings": [],
            },
            expires_at=batch.created_at + timedelta(minutes=10),
        ),
    )


@ai_tool(
    AiToolMeta(
        name="user.import_execute",
        agent="user_mgmt",
        summary=("Gateway-only execution for an approved user import preview."),
        required_perms=("system:user:import",),
        risk="high",
        readonly=False,
        idempotent=True,
        hitl_always=True,
        llm_visible=False,
        dry_run_supported=False,
        result_view="rows_affected",
        args_summary_fields=("reason", "on_conflict", "sync_mode"),
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
    """执行已经预览并确认的用户导入。

    **强制 HITL**（hitl_always=True）：用户必须在抽屉确认 preview summary 后才执行。
    模型不能跳过预览直接执行。

    流程：
    1. 凭 preview_token 反查 batch（含 file_sha256 + records_hash + operator_id）
    2. 从 sys_file 的存储引用重新加载文件
    3. parse_import_excel → records（与 preview 时 hash 一致）
    4. batch_create_users_from_records(...) → ImportResult
    5. ToolResult.rows_affected（successCount + skippedCount + ...）

    Args:
        preview_token: 来自 user.import_preview 返回值，10min TTL
        reason: 必须与预览时的业务理由一致
        on_conflict: 必须与预览时一致
        sync_mode: 'CREATE_ONLY'（默认）/ 'UPDATE_PROFILE' / 'FULL_SYNC'
    """
    from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
        user_role_assignment_service,
    )
    from app.modules.system.user.constants import EmployeeNoSyncMode  # noqa: PLC0415
    from app.modules.system.user.import_parser import (  # noqa: PLC0415
        import_file_has_column,
        parse_import_excel,
    )
    from app.modules.system.user.import_service import (  # noqa: PLC0415
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

    # 2. 凭 batch.file_storage_key 从 FileStorage 读 file_bytes
    # 执行阶段通过 FileStorage 抽象读取，不直接拼接文件系统路径。
    from app.core.file_storage import get_file_storage  # noqa: PLC0415

    if not batch.file_storage_key:
        from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

        raise BusinessRuleException(
            "批次未关联上传文件，无法 execute（请重新 import_preview）",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    storage = get_file_storage()
    try:
        file_bytes = await storage.read(batch.file_storage_key)
    except FileNotFoundError:
        from app.core.exceptions import BusinessRuleException  # noqa: PLC0415

        raise BusinessRuleException(
            f"预检文件已丢失（{batch.filename}），请重新 import_preview",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        ) from None
    filename = batch.filename or ""

    # 3. parse + execute
    mime_type = _user_import_mime_for_filename(filename)
    has_role_column = import_file_has_column(
        file_bytes,
        mime_type,
        "role_input",
    )
    await user_role_assignment_service.ensure_import_permissions(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        has_role_column=has_role_column,
    )
    records = parse_import_excel(
        file_bytes,
        mime_type,
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
        has_role_column=has_role_column,
    )

    return ToolResult.success(
        data={
            "successCount": result.success_count,
            "skippedCount": result.skipped_count,
            "overwrittenCount": result.overwritten_count,
            "failedCount": result.failed_count,
            "batchId": result.batch_id,
        },
        projection=_result_projection("user_import_batch", [result.batch_id]),
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


# ============ user.export ============


@ai_tool(
    AiToolMeta(
        name="user.export",
        agent="user_mgmt",
        summary=(
            "Export xlsx → {exportId,rowCount,downloadReady}. "
            "Reason required; filters: name/email/status."
        ),
        required_perms=("system:user:export",),
        risk="high",
        readonly=False,  # 写 ExportTask 表 + 生成 xlsx 文件
        idempotent=False,
        projection_kind="scope_bound",
        produces_file=True,
        dry_run_supported=True,
        # 导出结果使用详情卡，并提供鉴权下载地址。
        result_view="detail_card",
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
    status: Literal["1", "2"] | None = None,
) -> ToolResult:
    """导出用户列表到 Excel。

    始终创建 ExportTask、冻结筛选快照，并设置 30 天文件有效期。
    行数 > USER_EXPORT_ASYNC_THRESHOLD（5000）抛 AI_EXPORT_ASYNC_REQUIRED，
    用户必须缩窄筛选条件或拆分请求；当前不会自动入队。

    Args:
        reason: 业务理由（必填，1-256 字符）
        user_name / nickname / user_email / user_phone: filter（可选）
        status: '1' (启用) / '0' (禁用)，None=不过滤
    """
    from app.modules.ai.service.result_projection_service import (  # noqa: PLC0415
        result_projection_service,
    )
    from app.modules.system.user.export_service import (  # noqa: PLC0415
        export_users_to_excel,
        get_export_task,
        get_file_storage,
    )
    from app.modules.system.user.schemas import UserExportFilter  # noqa: PLC0415

    filter_ = UserExportFilter(
        user_name=user_name,
        nickname=nickname,
        user_email=user_email,
        user_phone=user_phone,
        status=status,
    )

    # Authorize before creating the database task or writing an external file.
    preflight_lineage = result_projection_service.freeze_lineage(
        tenant_id=ctx.tenant_id,
        agent_code=ctx.tool_meta.agent,
        tool_codes=[ctx.tool_meta.name],
        subject_refs=[],
        data_scope_hash=ctx.data_scope_hash,
        projection_dependency_message_ids=(ctx.projection_dependency_message_ids),
    )
    if not await result_projection_service.authorize_result_projection(
        ctx.db,
        ctx.user,
        owner_user_id=ctx.user.user_id,
        lineage=preflight_lineage,
    ):
        raise AuthorizationException(error_code="AI_RESULT_PROJECTION_FORBIDDEN")

    _xlsx_bytes, row_count, export_id = await export_users_to_excel(
        ctx.db,
        filter_,
        ctx.user,
        reason=reason,
    )

    # 从持久化任务读取文件元数据和到期时间，避免前端自行推断。
    task = await get_export_task(
        ctx.db,
        export_id,
        operator_id=ctx.user.user_id,
    )
    file_size = task.file_size_bytes if task else None
    expires_at = (task.created_at + timedelta(days=30)).isoformat() if task else None
    projection = _result_projection("user_export_task", [export_id], scope_bound=True)

    lineage = result_projection_service.freeze_lineage(
        tenant_id=ctx.tenant_id,
        agent_code=ctx.tool_meta.agent,
        tool_codes=[ctx.tool_meta.name],
        subject_refs=projection.subject_refs,
        data_scope_hash=ctx.data_scope_hash,
        projection_dependency_message_ids=(ctx.projection_dependency_message_ids),
    )
    download_token = await result_projection_service.issue_download_token(
        ctx.db,
        ctx.user,
        resource_type="user_export",
        resource_id=export_id,
        lineage=lineage,
    )
    if download_token is None:
        if task is not None and task.file_storage_key:
            await get_file_storage().delete(task.file_storage_key)
        raise AuthorizationException(error_code="AI_RESULT_PROJECTION_FORBIDDEN")
    download_url = f"/ai/download/user-export/{export_id}?token={download_token}"

    return ToolResult.success(
        data={
            "exportId": export_id,
            "rowCount": row_count,
            "downloadReady": True,
        },
        projection=projection,
        ui=UIResult(
            view_type="detail_card",
            view_data={
                "title": "用户导出",
                "fields": [
                    {"label": "ai.tool.field.exportId", "value": export_id},
                    {"label": "ai.tool.field.exportRows", "value": str(row_count)},
                    {
                        "label": "ai.tool.field.fileSize",
                        "value": f"{file_size} B" if file_size is not None else "—",
                    },
                    {"label": "ai.tool.field.expiresAt", "value": expires_at or "—"},
                ],
                "downloadUrl": download_url,
                "downloadFilename": (
                    f"hohu_users_{task.created_at.strftime('%Y%m%d_%H%M%S')}.xlsx"
                    if task
                    else "hohu_users.xlsx"
                ),
                "rowCount": row_count,
                "fileSize": file_size,
                "expiresAt": expires_at,
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
    status: Literal["1", "2"] | None = None,
) -> Any:
    """预估导出行数供确认界面展示。

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
                "请缩窄 filter 后重试"
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
