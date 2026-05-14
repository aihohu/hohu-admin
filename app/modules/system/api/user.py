from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.user import (
    ChangePassword,
    ProfileOut,
    ResetPassword,
    UpdateProfile,
    UserCreate,
    UserItemOut,
    UserQuery,
    UserUpdate,
)
from app.modules.system.service.dept_service import dept_service
from app.modules.system.service.user_service import user_service

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

    # 转换为 Schema 对象 (处理角色和部门简化)
    user_list = []
    for u in page_data.records:
        item = UserItemOut.model_validate(u)
        item.roles = [r.role_code for r in u.roles]
        # 部门信息解析
        if u.depts:
            item.dept_ids = [str(d.dept_id) for d in u.depts]
            item.dept_names = ", ".join(d.dept_name for d in u.depts)
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
    await user_service.update_user(db, user_id, user_in)
    await db.commit()

    # 处理部门关联
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
