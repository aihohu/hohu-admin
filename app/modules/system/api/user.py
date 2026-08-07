from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import IS_PRIMARY_YES
from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.exceptions import (
    BusinessRuleException,
    NotFoundException,
    UnprocessableEntityException,
)
from app.core.rbac import is_super_admin
from app.db.base import user_depts
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.user import (
    ChangePassword,
    ProfileOut,
    ResetPassword,
    UpdateProfile,
    UserCreate,
    UserDeptItem,
    UserItemOut,
    UserQuery,
    UserUpdate,
)
from app.modules.system.service.config_service import config_service
from app.modules.system.service.dept_service import dept_service
from app.modules.system.service.user_service import user_service
from app.modules.system.user.constants import EmployeeNoSyncMode
from app.modules.system.user.export_service import (
    download_export_file,
    export_users_to_excel,
    get_export_task,
    list_export_tasks,
)
from app.modules.system.user.import_parser import (
    MAX_FILE_SIZE_BYTES,
    ImportErrorCollection,
    parse_import_excel,
)
from app.modules.system.user.import_service import (
    batch_create_users_from_records,
    cancel_batch,
    dry_run_import_users,
    get_batch_detail,
    list_batch_logs,
    list_batches,
)
from app.modules.system.user.schemas import (
    ReasonSchema,
    UserExportFilter,
    UserExportRequest,
    UserExportTaskQuery,
    UserExportTaskResponse,
    UserImportBatchCancelResponse,
    UserImportBatchLogItem,
    UserImportBatchQuery,
    UserImportBatchResponse,
)
from app.modules.system.user.template_service import generate_import_template

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[UserItemOut]],
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
    # 调用 Service 层获取分页数据（含数据权限过滤）
    page_data = await user_service.get_user_list(db, query, current_user=_current_user)

    # 批量查询当前页用户的部门关联（含 is_primary 标记）
    user_ids = [u.user_id for u in page_data.records]
    user_depts_map: dict[int, list[tuple[int, bool]]] = {}
    if user_ids:
        stmt = select(
            user_depts.c.user_id,
            user_depts.c.dept_id,
            user_depts.c.is_primary,
        ).where(user_depts.c.user_id.in_(user_ids))
        result = await db.execute(stmt)
        for uid, did, is_primary in result.all():
            user_depts_map.setdefault(uid, []).append(
                (did, is_primary == IS_PRIMARY_YES)
            )

    # 转换为 Schema 对象 (处理角色和部门简化)
    user_list = []
    for u in page_data.records:
        item = UserItemOut.model_validate(u)
        item.roles = [r.role_code for r in u.roles]
        item.role_names = [r.role_name for r in u.roles]
        # 部门信息解析
        if u.depts:
            item.dept_ids = [str(d.dept_id) for d in u.depts]
            item.dept_names = ", ".join(d.dept_name for d in u.depts)
        # 部门关联（含主部门标记）
        dept_pairs = user_depts_map.get(u.user_id, [])
        if dept_pairs:
            item.user_depts = [
                UserDeptItem(dept_id=str(did), is_primary=is_primary)
                for did, is_primary in dept_pairs
            ]
            primary = next((str(did) for did, p in dept_pairs if p), None)
            item.primary_dept = primary
        user_list.append(item)

    # 返回分页包装结果
    return ResponseModel.success(
        data=PageResult(
            records=user_list,
            total=page_data.total,
            current=page_data.current,
            size=page_data.size,
        )
    )


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
):
    """
    创建新用户

    Args:
        user_in: 用户创建信息，包含用户名、昵称、邮箱、手机号、性别、状态、密码和角色列表
        db: 异步数据库会话

    Returns:
        ResponseModel: 创建成功的消息

    Request Body Fields:
        - user_name: 账号（必填，字母数字，4-50字符）
        - nickname: 昵称（可选，最大50字符）
        - user_email: 邮箱（必填，自动验证格式）
        - user_phone: 手机号（必填，11位中国手机号）
        - user_gender: 用户性别（必填，0-未知，1-男，2-女）
        - status: 用户状态（必填，1-启用，2-禁用）
        - password: 密码（必填，6-20字符，必须包含大小写字母和数字）
        - roles: 角色编码列表（必填，至少分配一个角色）
    """
    # 系统策略校验：是否强制用户必须有主部门
    if await config_service.get_bool(db, "user_require_primary_dept"):
        if not user_in.dept_ids or not any(d.is_primary for d in user_in.dept_ids):
            raise BusinessRuleException(
                "系统已开启「强制用户主部门」，必须为用户分配一个主部门",
                error_code="USER_PRIMARY_DEPT_REQUIRED",
            )

    new_user = await user_service.create_user(db, user_in)

    # 处理部门关联
    if user_in.dept_ids:
        dept_list = [
            {"dept_id": d.dept_id, "is_primary": d.is_primary} for d in user_in.dept_ids
        ]
        await dept_service.update_user_depts(db, new_user.user_id, dept_list)

    await db.commit()
    return ResponseModel.success(msg="创建成功")


@router.get(
    "/profile",
    response_model=ResponseModel[ProfileOut],
    summary="获取个人信息",
    description="获取当前登录用户的详细信息",
)
async def get_profile(current_user: User = Depends(get_current_user)):
    data = user_service.get_profile(current_user)
    return ResponseModel.success(data=data)


@router.put("/profile", summary="更新个人信息", description="用户更新自己的基本信息")
async def update_profile(
    body: UpdateProfile,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_service.update_profile(current_user, body)
    await db.commit()
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
):
    user_service.change_password(current_user, body)
    await db.commit()
    return ResponseModel.success(msg="密码修改成功")


@router.put(
    "/{user_id}",
    summary="修改用户",
    description="更新指定用户的基本信息，密码为可选参数",
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
    db: AsyncSession = Depends(get_db),
):
    """
    更新用户信息

    Args:
        user_id: 用户ID（路径参数）
        user_in: 用户更新信息，所有字段都是可选的
        db: 异步数据库会话

    Returns:
        ResponseModel: 更新成功的消息

    Path Parameters:
        - user_id: 要更新的用户ID

    Request Body Fields (Optional):
        - user_name: 账号
        - nickname: 昵称
        - user_email: 邮箱
        - user_phone: 手机号
        - user_gender: 用户性别
        - status: 用户状态
        - password: 密码（如果提供，会重新加密存储）
        - roles: 角色编码列表
    """
    # 处理部门关联（dept_ids 为 None 表示不改部门）
    # 先校验 dept_ids 再 update user：保证校验失败时 user 表不会被部分
    # commit，整个事务保持原子性。
    if user_in.dept_ids is not None:
        if await config_service.get_bool(db, "user_require_primary_dept"):
            if not any(d.is_primary for d in user_in.dept_ids):
                raise BusinessRuleException(
                    "系统已开启「强制用户主部门」，必须为用户分配一个主部门",
                    error_code="USER_PRIMARY_DEPT_REQUIRED",
                )

    await user_service.update_user(db, user_id, user_in)

    if user_in.dept_ids is not None:
        dept_list = [
            {"dept_id": d.dept_id, "is_primary": d.is_primary} for d in user_in.dept_ids
        ]
        await dept_service.update_user_depts(db, user_id, dept_list)

    await db.commit()
    return ResponseModel.success(msg="更新成功")


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
    db: AsyncSession = Depends(get_db),
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
    await user_service.reset_password(db, user_id, reset_in)
    await db.commit()
    return ResponseModel.success(msg="密码重置成功")


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
    db: AsyncSession = Depends(get_db),
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
    await user_service.delete_user(db, user_id)
    await db.commit()
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
    deleted_count = await user_service.batch_delete_users(db, ids, current_user.user_id)
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 个用户")


# ========== 用户导入（spec §5.1，Task 12）==========

#: spec §2.19 line 534 + import_service._PREVIEW_REDIS_TTL_SECONDS
_PREVIEW_TOKEN_TTL_SECONDS = 600


def _validate_import_reason(reason: str) -> str:
    """spec §2.30 v2.2 P1-3：reason 必填，1-256 字符（API 层入口校验）。

    service 层有 defense-in-depth（dry_run_import_users /
    batch_create_users_from_records 内部都校验），这里负责把
    Pydantic 未拦截的全空白场景拦下。
    """
    if reason is None:
        raise UnprocessableEntityException(
            "reason 必填（spec §2.30）",
            error_code="AI_IMPORT_REASON_REQUIRED",
        )
    stripped = reason.strip()
    if not stripped or len(stripped) > 256:
        raise UnprocessableEntityException(
            "reason 必填且长度 1-256 字符（spec §2.30）",
            error_code="AI_IMPORT_REASON_REQUIRED",
        )
    return stripped


def _coerce_dry_run(raw: str | None) -> bool:
    """把 multipart Form 的 dry_run 字符串转 bool（spec §5.1 line 2130）。

    兼容 ``"true"``/``"false"``（spec 原文）/ ``"1"``/``"0"`` /
    空值（视为 false）。其他非法值视为 false（不抛错，避免抢占业务异常）。
    """
    if raw is None:
        return False
    return raw.strip().lower() in {"true", "1", "yes"}


@router.post(
    "/import",
    summary="批量导入用户（dry_run=true 预检 / dry_run=false 正式导入）",
    description=(
        "spec §5.1：multipart 上传 Excel + on_conflict + sync_mode + reason + "
        "dry_run + preview_token。dry_run=true 跑预检 + 生成 preview_token；"
        "dry_run=false 凭 preview_token 落库（幂等重放见 §2.27）。"
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
    reason: Annotated[str, Form(description="业务理由（1-256 字符，spec §2.30）")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    on_conflict: Annotated[
        Literal["skip", "overwrite", "fail_fast"],
        Form(description="冲突处理策略，默认 skip"),
    ] = "skip",
    sync_mode: Annotated[
        Literal["CREATE_ONLY", "UPDATE_PROFILE", "FULL_SYNC"],
        Form(description="employee_no 同步策略（spec §2.24），默认 CREATE_ONLY"),
    ] = "CREATE_ONLY",
    dry_run: Annotated[
        str | None,
        Form(description='预检模式，传 "true" 跑预检并返回 preview_token'),
    ] = None,
    preview_token: Annotated[
        str | None,
        Form(description="dry_run=false 时必填，spec §2.19 三重校验用"),
    ] = None,
):
    """批量导入用户（spec §5.1，Task 12）

    流程：
    1. 入口校验 reason（spec §2.30）
    2. 读 UploadFile bytes + Content-Type
    3. ``parse_import_excel`` 解析 + 字段校验（spec §2.10 / §2.12）
       - 字段错误抛 ``ImportErrorCollection`` → API 层 catch 转 400 + errors[]
    4. 分流：
       - dry_run=true → ``dry_run_import_users`` 返回 preview_token + 四象限计数
       - dry_run=false → 校验 preview_token → ``batch_create_users_from_records``
    5. ``await db.commit()``（spec §3.6：API 层负责 commit）
    6. 返回 ``ResponseModel.success(data=...)``，data 含 camelCase 字段
    """
    reason_clean = _validate_import_reason(reason)

    # UploadFile → bytes + mime（spec §2.10 MIME 白名单在 parser 内校验）
    # Read at most one byte beyond the hard parser cap.  Calling read() without
    # a bound would let an oversized multipart body exhaust worker memory before
    # the parser can return AI_IMPORT_FILE_TOO_LARGE.
    file_bytes = await file.read(MAX_FILE_SIZE_BYTES + 1)
    mime_type = file.content_type or ""

    # 解析 + 字段校验（spec §2.10 / §2.12）
    try:
        records = parse_import_excel(file_bytes, mime_type)
    except ImportErrorCollection as exc:
        # 字段错误：转 400 + errorCode=AI_IMPORT_FIELD_ERRORS + errors[]（spec §2.12）
        # 全局 exception handler 只识别 BusinessException，所以这里手动构造
        wrapped = BusinessRuleException(
            f"{len(exc.errors)} 个字段错误",
            error_code="AI_IMPORT_FIELD_ERRORS",
        )
        wrapped.data = {
            "errors": [e.model_dump(mode="json", by_alias=True) for e in exc.errors]
        }
        raise wrapped from exc

    if _coerce_dry_run(dry_run):
        # dry_run 路径（spec §5.1 line 2136-2151）
        result, batch = await dry_run_import_users(
            db,
            records,
            current_user,
            file_bytes=file_bytes,
            filename=file.filename or "users.xlsx",
            reason=reason_clean,
            on_conflict=on_conflict,
        )
        await db.commit()

        # 构造响应：result（四象限）+ previewToken + expiresAt（spec §5.1）
        # batch.summary_* 是真实计数（防 records 截断后 count 漂移）
        # result 的 records list 已截断（spec §3.2 MAX_PREVIEW_RECORDS）
        result_data = result.model_dump(mode="json", by_alias=True)
        result_data.update(
            {
                "newCount": batch.summary_new,
                "existsCount": batch.summary_exists,
                "conflictCount": batch.summary_conflict,
                "outOfScopeCount": batch.summary_out_of_scope,
                "previewToken": batch.preview_token,
                "expiresAt": (
                    batch.created_at + timedelta(seconds=_PREVIEW_TOKEN_TTL_SECONDS)
                ).isoformat(),
            }
        )
        return ResponseModel.success(data=result_data)

    # execute 路径（spec §5.1 line 2156-2175）
    if not preview_token or not preview_token.strip():
        # spec §5.1 line 2180：dry_run=false 缺 preview_token → 422
        raise UnprocessableEntityException(
            "dry_run=false 时 preview_token 必填（spec §2.19）",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    result = await batch_create_users_from_records(
        db,
        records,
        preview_token=preview_token.strip(),
        file_bytes=file_bytes,
        filename=file.filename or "users.xlsx",
        reason=reason_clean,
        current_user=current_user,
        on_conflict=on_conflict,
        sync_mode=EmployeeNoSyncMode(sync_mode),
    )
    await db.commit()

    return ResponseModel.success(data=result.model_dump(mode="json", by_alias=True))


@router.get(
    "/import/template",
    summary="下载用户导入 Excel 模板（4 sheet + DataValidation）",
    description=(
        "spec §5.3 + §2.13 + §2.16：返 xlsx，含 4 sheet（数据 / 说明 / 部门字典 / 角色字典），"
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
):
    """spec §5.3：下载导入模板。

    流程：
    1. ``generate_import_template`` 内部查 sys_dept / sys_role，构造 4 sheet xlsx
    2. ``Response(content=bytes, media_type=xlsx)`` + Content-Disposition
       （决策 13.1：用 Response 而非 StreamingResponse 避免 audit_middleware 冲突）
    """
    xlsx_bytes = await generate_import_template(db)
    return Response(
        content=xlsx_bytes,
        media_type=_EXPORT_MIME_TYPE,
        headers={
            "Content-Disposition": f"attachment; filename={_TEMPLATE_FILENAME}",
        },
    )


# ========== 用户导入批次详情（spec §5.4 v2.2 P2，Task 15）==========

#: spec §5.4 line 2262 + §2.19：preview_token 10min TTL（CREATED/PREVIEW_DONE 用）
_PREVIEW_TTL_SECONDS = 600

#: spec §3.x 失败行文件 24h TTL（终态批次文件保留 1 天）
_FINISHED_TTL_SECONDS = 24 * 3600


def _compute_batch_expires_at(batch) -> datetime | None:
    """按 batch 状态动态算 expires_at（spec §5.4 line 2262 + 决策 15.5）。

    - CREATED / PREVIEW_DONE / RUNNING：created_at + 10min（preview 窗口，spec §2.19）
    - SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED：
      finished_at + 24h（finished_at 缺失回退 created_at + 24h）

    Args:
        batch: UserImportBatch ORM（已包含 status / created_at / finished_at）

    Returns:
        过期时间 datetime；batch 或 created_at 缺失时返回 None
    """
    if batch is None or batch.created_at is None:
        return None
    from app.modules.system.user.constants import ImportBatchStatus  # noqa: PLC0415

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
    """构造 GET /import/{batch_id} 响应 dict（spec §5.4 line 2238-2264）。

    安全：剥离 preview_token / file_sha256 / records_hash / reason
    （决策 15.4：reason 仅审计链路保留；preview_token 是 execute 凭证不能泄露）。
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
        sync_mode=None,  # 决策 15.6：暂不查 batch_log，spec §5.4 字段保留 None
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
    summary="分页查询导入批次列表（v2.2 P2）",
    description=(
        "spec §5.4 v2.2 P2 line 2272-2278：admin 查「我/团队导入过的批次列表」。"
        "支持 operator_id / status / created_at 时间窗过滤。"
        "权限：system:user:list（spec §5.4 line 2234，同 GET /import/{batch_id}）。"
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
):
    """spec §5.4 line 2272-2278：分页查询导入批次列表。

    流程：
    1. ``list_batches(db, query)`` outerjoin sys_user + 按 created_at DESC 排序
    2. 每个 batch 复用 ``_build_batch_response``（剥离敏感字段 + 动态算 expires_at）
    → PageResult[UserImportBatchResponse]
    """
    rows, total = await list_batches(db, query)
    records = [
        _build_batch_response(batch, operator_name) for batch, operator_name in rows
    ]
    return ResponseModel.success(
        data=PageResult(
            records=records,
            total=total,
            current=query.current,
            size=query.size,
        )
    )


@router.get(
    "/import/{batch_id}",
    response_model=ResponseModel[UserImportBatchResponse],
    summary="按 batch_id 查询导入批次详情",
    description=(
        "spec §5.4 v2.2 P2 line 2229-2278：导入批次状态查询，前端导入历史 + "
        "Phase 3 异步轮询 + 审计反查（batch_id 来自 sys_operation_log 反查）。"
        "权限：system:user:list（spec §5.4 line 2234：list 即可，因为查的是导入历史不是用户敏感数据）。"
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
):
    """spec §5.4 line 2229-2278：按 batch_id 查批次详情。

    流程：
    1. ``get_batch_detail(db, batch_id)`` → ``(batch, operator_name)``（outerjoin sys_user）
    2. batch 为 None → 抛 ``AI_IMPORT_BATCH_NOT_FOUND``
    3. 构造响应（含 expires_at 动态计算，剥离敏感字段）
    """
    batch, operator_name = await get_batch_detail(db, batch_id)
    if batch is None:
        raise NotFoundException(
            "用户导入批次",
            error_code="AI_IMPORT_BATCH_NOT_FOUND",
        )
    return ResponseModel.success(data=_build_batch_response(batch, operator_name))


@router.get(
    "/import/{batch_id}/logs",
    response_model=ResponseModel[PageResult[UserImportBatchLogItem]],
    summary="按 batch_id 查询导入批次操作日志（v2.2 P2）",
    description=(
        "spec §5.5 v2.2 P2 line 2280-2288 + §2.28：批次操作日志查询，支持 event 过滤 + 分页。"
        "权限：system:user:list（spec §5.5 line 2284，同 GET /import/{batch_id}）。"
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
            "EXECUTE_FINISH / EXECUTE_FAILED / EXPIRED / CANCELLED（spec §2.28 line 1252）"
        ),
    ),
    current: int = Query(1, ge=1, description="页码（1-based）"),
    size: int = Query(10, ge=1, le=100, description="每页数量（1-100）"),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """spec §5.5 line 2280-2288：批次操作日志查询。

    流程：
    1. 先 ``get_batch_detail`` 校验 batch 存在（404 一致性，与 GET /import/{batch_id} 对齐）
    2. ``list_batch_logs`` outerjoin sys_user + 按 created_at ASC 排序返回 [(log, operator_name), ...]
    3. 构造 UserImportBatchLogItem 列表 → PageResult
    """
    batch, _ = await get_batch_detail(db, batch_id)
    if batch is None:
        raise NotFoundException(
            "用户导入批次",
            error_code="AI_IMPORT_BATCH_NOT_FOUND",
        )

    rows, total = await list_batch_logs(
        db, batch_id, event=event, current=current, size=size
    )
    records = [
        UserImportBatchLogItem(
            log_id=log.log_id,
            event=log.event,
            from_status=log.from_status.value if log.from_status else None,
            to_status=log.to_status.value if log.to_status else None,
            detail=log.detail,
            operator_id=log.operator_id,
            operator_name=operator_name,
            created_at=log.created_at,
        )
        for log, operator_name in rows
    ]
    return ResponseModel.success(
        data=PageResult(records=records, total=total, current=current, size=size)
    )


@router.post(
    "/import/{batch_id}/cancel",
    response_model=ResponseModel[UserImportBatchCancelResponse],
    summary="取消导入批次（v2.2 P2）",
    description=(
        "spec §5.6 v2.2 P2 line 2290-2299 + §2.29：取消导入批次。两种场景："
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
):
    """spec §5.6 line 2290-2299：取消导入批次。

    流程：
    1. ``cancel_batch`` 处理两种场景 + 权限校验 + 状态校验
    2. API 层 ``db.commit``（CLAUDE.md：service 不 commit）
    3. 构造响应：``status`` 反映当前 batch.status（CANCELLED 或 RUNNING）

    reason：spec §2.30 v2.2 P1-3 必填，1-256 字符（ReasonSchema 入口校验）。
    """
    batch = await cancel_batch(db, batch_id, current_user, reason=body.reason)
    await db.commit()
    return ResponseModel.success(
        data=UserImportBatchCancelResponse(
            batch_id=batch.batch_id,
            status=batch.status.value,
            cancelled_at=batch.finished_at or datetime.now(),
        )
    )


# ========== 用户导出（spec §5.2 + §2.31 P1-5，Task 13）==========

#: spec §5.2 line 2200：xlsx MIME（与 export_service._EXPORT_MIME_TYPE 对齐）
_EXPORT_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: spec §5.3 line 2213：导入模板文件名（固定，不含日期 — 模板不分版本）
_TEMPLATE_FILENAME = "user_import_template.xlsx"


@router.post(
    "/export",
    summary="导出用户列表到 Excel（同步路径 ≤ 5000 行）",
    description=(
        "spec §5.2 + §2.31 P1-5：POST body 含 filter + reason（必填），"
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
):
    """spec §5.2 + §2.31 P1-5：同步导出。

    流程：
    1. UserExportRequest 校验（filter + reason，Pydantic 已拦空白 reason）
    2. export_users_to_excel 内部建 task → 查询 → 构造 Excel
       → service 层行数超阈值抛 AI_EXPORT_ASYNC_REQUIRED
    3. ``await db.commit()``（spec §3.6：API 层负责 commit）
    4. StreamingResponse + Content-Disposition: attachment; filename=users_YYYYMMDD.xlsx
    """
    filter_ = UserExportFilter(
        user_name=body.user_name,
        nickname=body.nickname,
        user_email=body.user_email,
        user_phone=body.user_phone,
        dept_id=body.dept_id,
        status=body.status,
    )
    xlsx_bytes, _row_count, _export_id = await export_users_to_excel(
        db,
        filter_,
        current_user,
        reason=body.reason,
    )
    await db.commit()

    # v2.3 §2.9.1 决策 30.6：hohu_ 前缀 + YYYYMMDD_HHmmss 时间戳避免同日多次导出冲突
    now = datetime.now()
    filename = f"hohu_users_{now.strftime('%Y%m%d_%H%M%S')}.xlsx"
    # 用 Response（非 StreamingResponse）：bytes 已全在内存，无流式收益；
    # BaseHTTPMiddleware（audit_middleware）与 StreamingResponse 冲突
    # （starlette 已知问题：BackgroundTask + receive hook 时序错乱），
    # Response 等价但兼容。spec §5.2 line 2200 「StreamingResponse」是契约
    # 描述（xlsx + Content-Disposition），实现细节用 Response 满足同样契约。
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
):
    """查询当前用户自己的导出列表；仅超管可跨 operator 查询。

    返回 PageResult，records 是 UserExportTaskResponse（operator_id 字符串化）。
    """
    page = await list_export_tasks(
        db,
        query,
        operator_id=current_user.user_id,
        allow_cross_owner=is_super_admin(current_user),
    )
    records = [UserExportTaskResponse.model_validate(t) for t in page.records]
    return ResponseModel.success(
        data=PageResult(
            records=records,
            total=page.total,
            current=page.current,
            size=page.size,
        )
    )


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
):
    """spec §2.31 line 1589-1591：导出任务详情查询。

    找不到抛 NotFoundException(AI_EXPORT_TASK_NOT_FOUND)。
    """
    task = await get_export_task(
        db,
        export_id,
        operator_id=current_user.user_id,
        allow_cross_owner=is_super_admin(current_user),
    )
    if task is None:
        raise NotFoundException(
            "用户导出任务",
            error_code="AI_EXPORT_TASK_NOT_FOUND",
        )
    return ResponseModel.success(data=UserExportTaskResponse.model_validate(task))


@router.get(
    "/export/{export_id}/download",
    summary="按 export_id 下载已导出文件（Task 33）",
    description=(
        "spec §2.31 line 1626 落地：AI 对话内 detail_card 下载按钮触发本端点。"
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
):
    """Task 33：AI 对话内点击下载闭环。

    权限与 POST /export 一致（system:user:export），且默认只能重下载本人创建的
    历史任务；仅超管可跨 operator 下载。

    决策 33.4：filename 从 task.created_at 派生（与同步导出决策 30.6
    一致），不重新生成当前时间 — 重下载历史任务时反映真实导出时刻，
    便于审计反查「这份文件是哪次导出的」。
    """
    xlsx_bytes, filename = await download_export_file(
        db,
        export_id,
        operator_id=current_user.user_id,
        allow_cross_owner=is_super_admin(current_user),
    )
    return Response(
        content=xlsx_bytes,
        media_type=_EXPORT_MIME_TYPE,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
