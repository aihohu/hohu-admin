"""User lifecycle and profile management AI tools."""

from typing import Any

from sqlalchemy import func, select

from app.constants import IS_PRIMARY_YES, STATUS_ENABLED, EnableStatus
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
)
from app.db.base import user_depts
from app.modules.ai.agents.gateway import ensure_targets_in_scope
from app.modules.ai.agents.gateway.result import (
    ResultProjection,
    ToolResult,
    UIResult,
)
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.stats_validator import (
    validate_filters_in_whitelist,
)
from app.modules.ai.core.context import AiToolContext
from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

from .common import (
    _coerce_list_limit,
    _result_projection,
    _validate_enable_status,
    _validate_enable_status_filter,
)


async def _get_ai_default_password(ctx: AiToolContext) -> str:
    """读取并校验仅后端可见的默认密码策略。"""
    from app.modules.system.service.user_service import (  # noqa: PLC0415
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
    from app.constants import USER_ROLE_CODE  # noqa: PLC0415
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
    status: EnableStatus,
    primary_dept_id: int,
    default_password: str,
) -> Any:
    """用 HTTP 同款 schema 校验 AI 创建参数，不回显敏感值。"""
    from pydantic import ValidationError  # noqa: PLC0415

    from app.modules.system.schemas.user import (  # noqa: PLC0415
        UserCreate,
        UserDeptItem,
    )

    canonical_status = _validate_enable_status(status)
    try:
        return UserCreate(
            user_name=user_name,
            nickname=nickname,
            user_email=user_email,
            user_phone=user_phone,
            user_gender=user_gender,
            status=canonical_status,
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
        required_perms=("system:user:add", "system:dept:list"),
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
    status: EnableStatus = EnableStatus.ENABLED,
) -> ToolResult:
    """创建单个用户；密码与角色完全由后端策略决定。"""
    from app.constants import USER_ROLE_CODE  # noqa: PLC0415
    from app.modules.system.service.user_department_assignment_service import (  # noqa: PLC0415
        user_department_assignment_service,
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
    await user_department_assignment_service.ensure_create_permissions(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        has_departments=True,
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
    await user_department_assignment_service.assign_created_user_departments(
        ctx.db,
        actor_user_id=ctx.user.user_id,
        target_user_id=new_user.user_id,
        dept_assignments=[(primary_dept_id, True)],
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
    status: EnableStatus = EnableStatus.ENABLED,
) -> Any:
    """预检创建目标、唯一性与后端默认策略，不写业务数据。"""
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
                    "display_value": dept.dept_name,
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
                "display_value": dept.dept_name,
            }
        ],
    )


async def _load_ai_reset_target(ctx: AiToolContext, *, user_id: int) -> User:
    """读取重置目标并应用保底账号/当前账号保护。"""
    from app.constants import (  # noqa: PLC0415
        ADMIN_USERNAME,
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
                "display_value": target.user_name,
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
    filters = _validate_enable_status_filter(
        validate_filters_in_whitelist(ctx.tool_meta, filters)
    )
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


async def _load_ai_user_department_assignments(
    ctx: AiToolContext,
    *,
    user_id: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Return the complete scoped assignment set or no partial identifiers."""
    assignment_rows = (
        await ctx.db.execute(
            select(user_depts.c.dept_id, user_depts.c.is_primary)
            .where(user_depts.c.user_id == user_id)
            .order_by(user_depts.c.dept_id)
        )
    ).all()
    dept_ids = {int(dept_id) for dept_id, _is_primary in assignment_rows}
    visible_dept_ids = ctx.data_scope.accessible_dept_ids
    if visible_dept_ids is not None and not dept_ids <= visible_dept_ids:
        return [], False
    if not dept_ids:
        return [], True

    depts = list(
        (
            await ctx.db.execute(
                select(Dept).where(Dept.dept_id.in_(dept_ids)).order_by(Dept.dept_id)
            )
        ).scalars()
    )
    if len(depts) != len(dept_ids):
        return [], False
    dept_map = {int(dept.dept_id): dept for dept in depts}
    return (
        [
            {
                "deptId": str(dept_id),
                "deptName": dept_map[int(dept_id)].dept_name,
                "isPrimary": str(is_primary) == IS_PRIMARY_YES,
                "status": dept_map[int(dept_id)].status,
            }
            for dept_id, is_primary in assignment_rows
        ],
        True,
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
        raise BusinessRuleException(
            "多个用户匹配当前条件，请补充 user_id 或更多精确条件",
            error_code="AI_LOOKUP_AMBIGUOUS",
        )

    u = user[0]
    result_data: dict[str, Any] = {
        "id": str(u.user_id),
        "user_name": u.user_name,
        "nickname": u.nickname,
        "status": u.status,
    }
    subject_refs: list[dict[str, str]] = [{"type": "user", "id": str(u.user_id)}]
    if "system:dept:list" in ctx.perms:
        assignments, assignments_complete = await _load_ai_user_department_assignments(
            ctx,
            user_id=int(u.user_id),
        )
        result_data["departmentAssignmentsComplete"] = assignments_complete
        result_data["departmentAssignments"] = assignments
        if assignments_complete:
            subject_refs.extend(
                {"type": "dept", "id": assignment["deptId"]}
                for assignment in assignments
            )
    if USER_ROLE_AUTH_PERMISSION in ctx.perms:
        from app.modules.system.service.user_role_assignment_service import (  # noqa: PLC0415
            user_role_assignment_service,
        )

        (
            roles,
            roles_complete,
        ) = await user_role_assignment_service.get_complete_assignable_roles(
            ctx.db,
            actor_user_id=ctx.user.user_id,
            target_user_id=int(u.user_id),
        )
        result_data["roleAssignmentsComplete"] = roles_complete
        result_data["roleAssignments"] = [
            {
                "roleId": str(role.role_id),
                "roleCode": role.role_code,
                "roleName": role.role_name,
                "dataScope": role.data_scope,
                "status": role.status,
            }
            for role in roles
        ]
        if roles_complete:
            subject_refs.append(
                {
                    "type": "complete_user_role_assignment",
                    "id": str(u.user_id),
                }
            )
            subject_refs.extend(
                {"type": "delegable_role", "id": str(role.role_id)} for role in roles
            )
        else:
            subject_refs.append(
                {"type": "user_role_assignment_access", "id": str(u.user_id)}
            )
    return ToolResult.success(
        data=result_data,
        projection=ResultProjection(subject_refs=tuple(subject_refs)),
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
    status: EnableStatus | None = None,
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
        NotFoundException,
    )

    # 至少一个可更新字段
    canonical_status = None if status is None else _validate_enable_status(status)
    update_payload = {
        "nickname": nickname,
        "user_email": user_email,
        "user_phone": user_phone,
        "user_gender": user_gender,
        "status": canonical_status,
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
    status: EnableStatus | None = None,
) -> Any:
    """列出待更新字段和当前值，供确认界面展示。"""
    from app.modules.ai.agents.hitl.constants import DryRunResult  # noqa: PLC0415

    canonical_status = None if status is None else _validate_enable_status(status)
    update_payload = {
        "nickname": nickname,
        "user_email": user_email,
        "user_phone": user_phone,
        "user_gender": user_gender,
        "status": canonical_status,
    }
    provided = {k: v for k, v in update_payload.items() if v is not None}
    if not provided:
        return DryRunResult(
            ok=False,
            count=0,
            reason="至少提供一个待更新字段",
        )

    await ensure_targets_in_scope(ctx, user_ids=[user_id])

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
                "display_value": user.user_name,
            },
            *[{"label": field, "value": value} for field, value in provided.items()],
        ],
    )
