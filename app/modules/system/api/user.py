from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import IS_PRIMARY_YES
from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.exceptions import BusinessRuleException, UnprocessableEntityException
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
from app.modules.system.user.import_parser import (
    ImportErrorCollection,
    parse_import_excel,
)
from app.modules.system.user.import_service import (
    batch_create_users_from_records,
    dry_run_import_users,
)

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
    file: Annotated[UploadFile, File(description="Excel 文件（≤ 10MB，xlsx/xls/csv）")],
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
    file_bytes = await file.read()
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
