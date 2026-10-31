from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.exceptions import (
    UnprocessableEntityException,
)
from app.core.tenant import TenantContext
from app.db.authorization_http import system_write, user_read
from app.db.authorization_transfer_http import transfer_read_http
from app.db.session import get_db
from app.modules.auth.service import (
    get_current_tenant_context,
)
from app.modules.system.models.user import User
from app.modules.system.schemas.user import (
    AssignableRoleOut,
    ChangePassword,
    ResetPassword,
    UpdateProfile,
    UserCreate,
    UserDepartmentUpdate,
    UserPatch,
    UserQuery,
    UserRoleUpdate,
    UserUpdate,
)
from app.modules.system.schemas.user_projection import ProjectedUserOut
from app.modules.system.schemas.user_transfer import (
    ReasonSchema,
    UserExportRequest,
    UserExportTaskQuery,
    UserExportTaskResponse,
    UserImportBatchCancelResponse,
    UserImportBatchLogItem,
    UserImportBatchQuery,
    UserImportBatchResponse,
)
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service,
)

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[ProjectedUserOut]],
    summary="获取用户列表分页",
    description="根据查询条件获取用户分页列表，支持按用户名、昵称、手机号、邮箱等条件筛选",
    responses={
        200: {"description": "获取成功，返回用户分页数据"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:user:list"))],
)
async def get_user_list(
    query: UserQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    获取用户分页列表

    Args:
        query: 查询参数，包含分页信息和筛选条件
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 包含用户分页列表的数据，每条用户信息包含角色列表

    Query Parameters:
        - current: 当前页码（默认1）
        - size: 每页数量（默认10，最大100）
        - user_name: 用户名（支持模糊查询）
        - nickname: 昵称（支持模糊查询）
        - user_phone: 手机号（支持模糊查询）
        - user_email: 邮箱（支持模糊查询）
        - user_gender: 用户性别（0-未知，1-男，2-女）
        - status: 用户状态（1-启用，2-禁用）
    """
    data = await user_read(
        db, _current_user, operation="list", query=query, tenant=tenant
    )
    return ResponseModel.success(data=data)


@router.post(
    "/add",
    summary="创建用户",
    description="创建新用户账号，包括用户基本信息、密码和角色分配",
    responses={
        200: {"description": "创建成功"},
        400: {"description": "参数验证失败或用户名已存在"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:user:add"))],
)
async def add_user(
    user_in: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """Create a user with optional explicit role IDs and department bindings."""
    await system_write(
        db, current_user, operation="user.create", payload=user_in, tenant=tenant
    )
    return ResponseModel.success(msg="创建成功")


@router.get(
    "/assignable-roles",
    response_model=ResponseModel[list[AssignableRoleOut]],
    summary="获取可委派角色",
    dependencies=[Depends(require_permissions("system:user:role-auth"))],
)
async def get_assignable_roles(
    query: Annotated[str | None, Query(max_length=50)] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    roles = await user_role_assignment_service.list_assignable_roles(
        db,
        actor_user_id=current_user.user_id,
        query=query,
        limit=limit,
        tenant=tenant,
    )
    return ResponseModel.success(
        data=[AssignableRoleOut.model_validate(role) for role in roles]
    )


@router.get(
    "/profile",
    response_model=ResponseModel[ProjectedUserOut],
    summary="获取个人信息",
    description="获取当前登录用户的详细信息",
)
async def get_profile(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    data = await user_read(db, current_user, operation="profile")
    return ResponseModel.success(data=data)


@router.get("/field-capabilities", summary="获取本人资料字段能力")
async def get_field_capabilities(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return ResponseModel.success(
        data=await user_read(db, current_user, operation="capabilities")
    )


@router.put("/profile", summary="更新个人信息", description="用户更新自己的基本信息")
async def update_profile(
    body: UpdateProfile,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await system_write(db, current_user, operation="user.profile", payload=body)
    return ResponseModel.success(msg="更新成功")


@router.put(
    "/change-password",
    summary="修改密码",
    description="用户修改自己的密码，需要验证当前密码",
)
async def change_password(
    body: ChangePassword,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await system_write(
        db, current_user, operation="user.change_password", payload=body, tenant=tenant
    )
    return ResponseModel.success(msg="密码修改成功")


@router.put(
    "/{user_id}",
    summary="修改用户",
    description="更新指定用户的基本信息；密码必须通过独立重置接口修改",
    responses={
        200: {"description": "更新成功"},
        400: {"description": "参数验证失败"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "用户不存在"},
    },
    dependencies=[Depends(require_permissions("system:user:edit"))],
)
async def update_user(
    user_id: int,
    user_in: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """Update profile fields only; role and department writers are separate."""
    await system_write(
        db,
        current_user,
        operation="user.update",
        payload=user_in,
        target_id=user_id,
        tenant=tenant,
    )
    return ResponseModel.success(msg="更新成功")


@router.patch(
    "/{user_id}",
    summary="部分更新用户资料",
    description="未携带字段保持原值；可空字段显式 null 清空；密码和关系使用专用接口。",
    dependencies=[Depends(require_permissions("system:user:edit"))],
)
async def patch_user(
    user_id: int,
    body: UserPatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await system_write(
        db,
        current_user,
        operation="user.update",
        payload=body,
        target_id=user_id,
        tenant=tenant,
    )
    return ResponseModel.success(msg="更新成功")


@router.put(
    "/{user_id}/roles",
    summary="替换用户角色",
    dependencies=[
        Depends(require_permissions("system:user:edit")),
        Depends(require_permissions("system:user:role-auth")),
    ],
)
async def update_user_roles(
    user_id: int,
    body: UserRoleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await system_write(
        db,
        current_user,
        operation="user.roles",
        payload=body.role_ids,
        target_id=user_id,
        tenant=tenant,
    )
    return ResponseModel.success(msg="角色更新成功")


@router.put(
    "/{user_id}/departments",
    summary="替换用户部门",
    dependencies=[
        Depends(require_permissions("system:user:edit")),
        Depends(require_permissions("system:dept:list")),
    ],
)
async def update_user_departments(
    user_id: int,
    body: UserDepartmentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await system_write(
        db,
        current_user,
        operation="user.departments",
        payload=[
            (int(item.dept_id), item.is_primary) for item in body.dept_assignments
        ],
        target_id=user_id,
        tenant=tenant,
    )
    return ResponseModel.success(msg="部门更新成功")


@router.put(
    "/{user_id}/reset-password",
    summary="重置用户密码",
    description="管理员重置指定用户的密码，无需提供旧密码",
    responses={
        200: {"description": "重置成功"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "用户不存在"},
    },
    dependencies=[Depends(require_permissions("system:user:reset-password"))],
)
async def reset_password(
    user_id: int,
    reset_in: ResetPassword,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    重置用户密码

    Args:
        user_id: 用户ID（路径参数）
        reset_in: 新密码信息
        db: 异步数据库会话

    Returns:
        ResponseModel: 重置成功的消息
    """
    await system_write(
        db,
        current_user,
        operation="user.password",
        payload=reset_in,
        target_id=user_id,
        tenant=tenant,
    )
    return ResponseModel.success(msg="密码重置成功")


@router.put(
    "/{user_id}/revoke-sessions",
    summary="撤销用户全部会话",
    description="递增用户认证版本，使已签发的 access/refresh token 全部失效",
    dependencies=[Depends(require_permissions("system:user:reset-password"))],
)
async def revoke_user_sessions(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await system_write(
        db, current_user, operation="user.revoke", target_id=user_id, tenant=tenant
    )
    return ResponseModel.success(msg="会话已全部撤销")


@router.delete(
    "/{user_id}",
    summary="删除用户",
    description="删除指定的单个用户账号",
    responses={
        200: {"description": "删除成功"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "用户不存在"},
    },
    dependencies=[Depends(require_permissions("system:user:delete"))],
)
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    删除单个用户

    Args:
        user_id: 用户ID（路径参数）
        db: 异步数据库会话

    Returns:
        ResponseModel: 删除成功的消息

    Path Parameters:
        - user_id: 要删除的用户ID

    Note:
        - 此操作不可逆，请谨慎操作
        - 用户删除后，关联的角色关系也会被清除
    """
    await system_write(
        db, current_user, operation="user.delete", target_id=user_id, tenant=tenant
    )
    return ResponseModel.success(msg="删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除用户",
    description="批量删除多个用户账号，支持传入用户ID列表",
    responses={
        200: {"description": "删除成功"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:user:batch-delete"))],
)
async def batch_delete_users(
    ids: list[int],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    批量删除用户

    Args:
        ids: 用户ID列表
        db: 异步数据库会话
        current_user: 当前登录用户对象（用于防止删除自己）

    Returns:
        ResponseModel: 删除成功的消息，包含实际删除的用户数量

    Request Body:
        - ids: 用户ID数组（如：[123456, 123457, 123458]）

    Note:
        - 此操作不可逆，请谨慎操作
        - 不允许删除当前登录用户
        - 用户删除后，关联的角色关系也会被清除
    """
    outcome = await system_write(
        db, current_user, operation="user.batch_delete", payload=ids, tenant=tenant
    )
    deleted_count = outcome["result"]
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 个用户")


# ========== 用户导入 ==========

#: 预检令牌有效期，与导入服务保持一致。
_PREVIEW_TOKEN_TTL_SECONDS = 600


def _validate_import_reason(reason: str) -> str:
    """在 API 入口校验导入原因必填且长度为 1-256 字符。

    service 层有 defense-in-depth（dry_run_import_users /
    batch_create_users_from_records 内部都校验），这里负责把
    Pydantic 未拦截的全空白场景拦下。
    """
    if reason is None:
        raise UnprocessableEntityException(
            "reason 必填",
            error_code="AI_IMPORT_REASON_REQUIRED",
        )
    stripped = reason.strip()
    if not stripped or len(stripped) > 256:
        raise UnprocessableEntityException(
            "reason 必填且长度 1-256 字符",
            error_code="AI_IMPORT_REASON_REQUIRED",
        )
    return stripped


def _coerce_dry_run(raw: str | None) -> bool:
    """把 multipart Form 的 ``dry_run`` 字符串转为布尔值。

    兼容 ``"true"``/``"false"``、``"1"``/``"0"`` 和
    空值（视为 false）。其他非法值视为 false（不抛错，避免抢占业务异常）。
    """
    if raw is None:
        return False
    return raw.strip().lower() in {"true", "1", "yes"}


@router.post(
    "/import",
    summary="批量导入用户（dry_run=true 预检 / dry_run=false 正式导入）",
    description=(
        "multipart 上传 Excel，并提交 on_conflict、sync_mode、reason、"
        "dry_run + preview_token。dry_run=true 跑预检 + 生成 preview_token；"
        "dry_run=false 凭 preview_token 落库，并支持幂等重放。"
    ),
    responses={
        200: {"description": "导入成功（dry_run 或正式）"},
        400: {"description": "字段格式错 / 文件大小超限 / MIME 非白名单"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        422: {"description": "preview_token 缺失/失效 / 批次状态非法"},
    },
    dependencies=[Depends(require_permissions("system:user:import"))],
)
async def import_users(
    file: Annotated[UploadFile, File(description="Excel 文件（≤ 10MB，xlsx/csv）")],
    reason: Annotated[str, Form(description="业务理由（1-256 字符）")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
    on_conflict: Annotated[
        Literal["skip", "overwrite", "fail_fast"],
        Form(description="冲突处理策略，默认 skip"),
    ] = "skip",
    sync_mode: Annotated[
        Literal["CREATE_ONLY", "UPDATE_PROFILE", "FULL_SYNC"],
        Form(description="employee_no 同步策略，默认 CREATE_ONLY"),
    ] = "CREATE_ONLY",
    dry_run: Annotated[
        str | None,
        Form(description='预检模式，传 "true" 跑预检并返回 preview_token'),
    ] = None,
    preview_token: Annotated[
        str | None,
        Form(description="dry_run=false 时必填，用于校验预检结果"),
    ] = None,
):
    """预检与正式导入均在独立授权 owner 中完成唯一事务。"""
    from app.db.authorization_transfer_http import import_http  # noqa: PLC0415

    result = await import_http(
        db,
        current_user,
        tenant=tenant,
        file=file,
        reason=_validate_import_reason(reason),
        on_conflict=on_conflict,
        sync_mode=sync_mode,
        dry_run=_coerce_dry_run(dry_run),
        preview_token=preview_token,
    )
    return ResponseModel.success(data=result)


@router.get(
    "/import/template",
    summary="下载用户导入 Excel 模板（4 sheet + DataValidation）",
    description=(
        "返回 xlsx，含 4 个 sheet（数据 / 说明 / 部门字典 / 角色字典），"
        "字典 sheet 实时查 sys_dept / sys_role。"
        "权限：system:user:import（同 import，避免未授权下载模板探查字段）。"
    ),
    responses={
        200: {"description": "xlsx 文件流"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:user:import"))],
)
async def download_import_template(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """下载用户导入模板。

    流程：
    1. ``generate_import_template`` 内部查 sys_dept / sys_role，构造 4 sheet xlsx
    2. ``Response(content=bytes, media_type=xlsx)`` + Content-Disposition
       使用 Response，避免 StreamingResponse 与审计中间件的时序冲突
    """
    xlsx_bytes = await transfer_read_http(
        db, _current_user, tenant=tenant, operation="template"
    )
    return Response(
        content=xlsx_bytes,
        media_type=_EXPORT_MIME_TYPE,
        headers={
            "Content-Disposition": f"attachment; filename={_TEMPLATE_FILENAME}",
        },
    )


# ========== 用户导入批次 ==========

#: 预检与执行中批次的有效期。
_PREVIEW_TTL_SECONDS = 600

#: 终态批次及失败行文件保留 24 小时。
_FINISHED_TTL_SECONDS = 24 * 3600


def _compute_batch_expires_at(batch) -> datetime | None:
    """根据批次状态动态计算过期时间。

    - CREATED / PREVIEW_DONE / RUNNING：created_at + 10 分钟
    - SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED：
      finished_at + 24h（finished_at 缺失回退 created_at + 24h）

    Args:
        batch: UserImportBatch ORM（已包含 status / created_at / finished_at）

    Returns:
        过期时间 datetime；batch 或 created_at 缺失时返回 None
    """
    if batch is None or batch.created_at is None:
        return None
    from app.modules.system.constants import ImportBatchStatus  # noqa: PLC0415

    preview_states = {
        ImportBatchStatus.CREATED,
        ImportBatchStatus.PREVIEW_DONE,
        ImportBatchStatus.RUNNING,
    }
    if batch.status in preview_states:
        return batch.created_at + timedelta(seconds=_PREVIEW_TTL_SECONDS)

    # 终态：优先用 finished_at，回退 created_at（CANCELLED 可能 finished_at 还没写）
    base = batch.finished_at or batch.created_at
    return base + timedelta(seconds=_FINISHED_TTL_SECONDS)


def _build_batch_response(batch, operator_name: str | None) -> dict:
    """构造导入批次详情响应。

    安全：剥离 preview_token / file_sha256 / records_hash / reason
    ``reason`` 仅保留在审计链路；``preview_token`` 是执行凭证，不能泄露。
    """
    payload = UserImportBatchResponse(
        batch_id=batch.batch_id,
        operator_id=batch.operator_id,
        operator_name=operator_name,
        filename=batch.filename,
        total_rows=batch.total_rows,
        summary_new=batch.summary_new,
        summary_exists=batch.summary_exists,
        summary_conflict=batch.summary_conflict,
        summary_out_of_scope=batch.summary_out_of_scope,
        success_count=batch.success_count,
        skipped_count=batch.skipped_count,
        overwritten_count=batch.overwritten_count,
        failed_count=batch.failed_count,
        failed_rows_file=batch.failed_rows_file,
        on_conflict=batch.on_conflict,
        sync_mode=None,  # 当前响应不额外查询批次日志，字段保持为空。
        status=batch.status.value
        if hasattr(batch.status, "value")
        else str(batch.status),
        created_at=batch.created_at,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        expires_at=_compute_batch_expires_at(batch),
    )
    return payload.model_dump(mode="json", by_alias=True)


@router.get(
    "/import",
    response_model=ResponseModel[PageResult[UserImportBatchResponse]],
    summary="分页查询导入批次列表",
    description=(
        "查询当前用户或团队执行过的导入批次。"
        "支持 operator_id / status / created_at 时间窗过滤。"
        "需要 system:user:list 权限。"
    ),
    responses={
        200: {"description": "分页批次列表"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        422: {"description": "非法 status 值"},
    },
    dependencies=[Depends(require_permissions("system:user:list"))],
)
async def list_import_batches(
    query: UserImportBatchQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    result = await transfer_read_http(
        db, _current_user, tenant=tenant, operation="batches", query=query
    )
    return ResponseModel.success(data=result)


@router.get(
    "/import/{batch_id}",
    response_model=ResponseModel[UserImportBatchResponse],
    summary="按 batch_id 查询导入批次详情",
    description=(
        "查询导入批次状态，供导入历史、异步轮询和审计反查使用。"
        "需要 system:user:list 权限；响应不包含用户敏感字段。"
    ),
    responses={
        200: {"description": "批次详情"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "batch_id 不存在"},
    },
    dependencies=[Depends(require_permissions("system:user:list"))],
)
async def get_import_batch_detail(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    result = await transfer_read_http(
        db, _current_user, tenant=tenant, operation="batch", identifier=batch_id
    )
    return ResponseModel.success(data=result)


@router.get(
    "/import/{batch_id}/logs",
    response_model=ResponseModel[PageResult[UserImportBatchLogItem]],
    summary="按 batch_id 查询导入批次操作日志",
    description=(
        "查询批次操作日志，支持按 event 过滤和分页。需要 system:user:list 权限。"
    ),
    responses={
        200: {"description": "日志列表（分页）"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "batch_id 不存在"},
    },
    dependencies=[Depends(require_permissions("system:user:list"))],
)
async def get_import_batch_logs(
    batch_id: str,
    event: str | None = Query(
        None,
        description=(
            "事件类型过滤：CREATED / PREVIEW_DONE / EXECUTE_START / CHUNK_PROGRESS / "
            "EXECUTE_FINISH / EXECUTE_FAILED / EXPIRED / CANCELLED"
        ),
    ),
    current: int = Query(1, ge=1, description="页码（1-based）"),
    size: int = Query(10, ge=1, le=100, description="每页数量（1-100）"),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    result = await transfer_read_http(
        db,
        _current_user,
        tenant=tenant,
        operation="logs",
        identifier=batch_id,
        query={"event": event, "current": current, "size": size},
    )
    return ResponseModel.success(data=result)


@router.post(
    "/import/{batch_id}/cancel",
    response_model=ResponseModel[UserImportBatchCancelResponse],
    summary="取消导入批次",
    description=(
        "取消导入批次。两种场景："
        "PREVIEW_DONE 直接 CAS 转 CANCELLED + 清理 preview 文件；"
        "RUNNING 设置 Redis cancel 标志，chunk loop 下一个 chunk 边界跳出 → "
        "PARTIAL_SUCCESS（协作式 cancel）。"
        "权限：system:user:import（必须是 batch operator 本人或超管）。"
    ),
    responses={
        200: {"description": "取消成功（已转 CANCELLED 或已设置 cancel 标志）"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "非 operator 本人且非超管"},
        404: {"description": "batch_id 不存在"},
        422: {
            "description": "reason 校验失败 / 状态不可取消（AI_IMPORT_BATCH_NOT_CANCELLABLE）"
        },
    },
    dependencies=[Depends(require_permissions("system:user:import"))],
)
async def cancel_import_batch(
    batch_id: str,
    body: ReasonSchema,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """取消提交与文件退役由固定 owner 分阶段完成。"""
    from app.db.authorization_transfer_http import cancel_http  # noqa: PLC0415

    result = await cancel_http(
        db, current_user, tenant=tenant, identifier=batch_id, reason=body.reason
    )
    return ResponseModel.success(data=result)


# ========== 用户导出 ==========

#: Excel MIME 类型，与导出服务保持一致。
_EXPORT_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: 导入模板使用固定文件名，便于客户端识别。
_TEMPLATE_FILENAME = "user_import_template.xlsx"


@router.post(
    "/export",
    summary="导出用户列表到 Excel（同步路径 ≤ 5000 行）",
    description=(
        "POST body 包含 filter 和必填的 reason，"
        "返回 StreamingResponse xlsx + Content-Disposition。"
        "行数 > USER_EXPORT_ASYNC_THRESHOLD（5000）抛 422 AI_EXPORT_ASYNC_REQUIRED。"
    ),
    responses={
        200: {"description": "xlsx 文件流"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        422: {"description": "reason 校验失败 / 行数 > 5000（异步阈值）"},
    },
    dependencies=[Depends(require_permissions("system:user:export"))],
)
async def export_users(
    body: UserExportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """返回授权 owner 唯一提交后的已核验 Excel。"""
    from app.db.authorization_transfer_http import export_http  # noqa: PLC0415

    xlsx_bytes, filename = await export_http(db, current_user, tenant=tenant, body=body)
    return Response(
        content=xlsx_bytes,
        media_type=_EXPORT_MIME_TYPE,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get(
    "/export",
    response_model=ResponseModel[PageResult[UserExportTaskResponse]],
    summary="分页查询导出任务列表",
    description=(
        "默认只查询当前用户创建的导出任务；超管可按 operator_id / status 过滤。"
    ),
    responses={
        200: {"description": "分页列表"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:user:list"))],
)
async def list_export_tasks_endpoint(
    query: UserExportTaskQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    result = await transfer_read_http(
        db, current_user, tenant=tenant, operation="exports", query=query
    )
    return ResponseModel.success(data=result)


@router.get(
    "/export/{export_id}",
    response_model=ResponseModel[UserExportTaskResponse],
    summary="按 export_id 查询导出任务详情",
    description="默认仅任务创建人可见；超管可跨 operator 审计反查。",
    responses={
        200: {"description": "任务详情"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "export_id 不存在或当前用户不可见"},
    },
    dependencies=[Depends(require_permissions("system:user:list"))],
)
async def get_export_task_detail(
    export_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    result = await transfer_read_http(
        db, current_user, tenant=tenant, operation="export", identifier=export_id
    )
    return ResponseModel.success(data=result)


@router.get(
    "/export/{export_id}/download",
    summary="按 export_id 下载已导出文件",
    description=(
        "AI 对话内的详情卡片下载按钮可调用本端点。"
        "从 task.file_storage_key 读 bytes → 流式返回。"
        "任务不存在 / 状态非 SUCCESS / 文件被删 → 各自 errorCode。"
    ),
    responses={
        200: {"description": "xlsx 文件流"},
        400: {"description": "任务未成功 / 文件缺失 / 文件已过期"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "export_id 不存在"},
    },
    dependencies=[Depends(require_permissions("system:user:export"))],
)
async def download_export_file_endpoint(
    export_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """处理 AI 对话内的导出文件下载。

    权限与 POST /export 一致（system:user:export），且默认只能重下载本人创建的
    历史任务；仅超管可跨 operator 下载。

    文件名从 ``task.created_at`` 派生，不重新生成当前时间；重下载历史任务时反映真实导出时刻，
    便于审计反查「这份文件是哪次导出的」。
    """
    from app.db.authorization_transfer_http import download_http  # noqa: PLC0415

    xlsx_bytes, filename = await download_http(
        db, current_user, tenant=tenant, identifier=export_id
    )
    return Response(
        content=xlsx_bytes,
        media_type=_EXPORT_MIME_TYPE,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
