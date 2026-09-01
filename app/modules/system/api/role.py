from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context
from app.modules.system.models.user import User
from app.modules.system.schemas.role import (
    RoleCreate,
    RoleOut,
    RoleQuery,
    RoleSummaryOut,
    RoleUpdate,
)
from app.modules.system.service.role_management_service import role_management_service
from app.modules.system.service.role_service import role_service

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[RoleSummaryOut]],
    summary="获取角色列表分页",
    description="根据查询条件获取角色分页列表，支持按角色名称、角色编码、数据权限和状态筛选",
    responses={
        200: {"description": "获取成功，返回角色分页数据"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:role:list"))],
)
async def list_roles(
    query: RoleQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    获取角色分页列表

    Args:
        query: 查询参数，包含分页信息和筛选条件
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 包含角色分页列表的数据

    Query Parameters:
        - current: 当前页码（默认1）
        - size: 每页数量（默认10，最大100）
        - role_name: 角色名称（支持模糊查询）
        - role_code: 角色编码（支持模糊查询）
        - data_scope: 数据权限范围（1-全部，2-自定义，3-本部门，4-本部门及以下，5-仅本人）
        - status: 角色状态（1-启用，2-禁用）
    """
    summaries, total, _contributors = await role_management_service.summarize_roles(
        db,
        actor_user_id=_current_user.user_id,
        tenant=tenant,
        role_name=query.role_name,
        role_code=query.role_code,
        data_scope=query.data_scope,
        status=query.status,
        limit=query.size,
        offset=(query.current - 1) * query.size,
    )
    page_data = PageResult[RoleSummaryOut](
        records=[RoleSummaryOut.model_validate(value) for value in summaries],
        total=total,
        current=query.current,
        size=query.size,
    )
    return ResponseModel.success(data=page_data)


@router.get(
    "/all",
    response_model=ResponseModel[list[RoleSummaryOut]],
    summary="获取全部角色列表(不分页)",
    description="获取系统中所有已启用的角色列表，不分页，用于下拉选择等场景",
    responses={
        200: {"description": "获取成功，返回角色列表"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:role:list"))],
)
async def get_all_roles(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    获取所有已启用的角色列表

    Args:
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 包含所有已启用角色的列表

    Note:
        - 仅返回状态为启用的角色
        - 不分页，返回所有符合条件的角色
        - 适合用于前端下拉选择框
    """
    roles, _total, _contributors = await role_management_service.summarize_roles(
        db,
        actor_user_id=_current_user.user_id,
        tenant=tenant,
        status="1",
        limit=10_000,
    )
    return ResponseModel.success(
        data=[RoleSummaryOut.model_validate(value) for value in roles]
    )


@router.get(
    "/menus/{role_id}",
    response_model=ResponseModel[list[str]],
    summary="获取角色菜单列表",
    dependencies=[Depends(require_permissions("system:role:menu-auth"))],
)
async def get_menus(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """获取角色的菜单ID列表"""
    await role_management_service.authorize_role_projection(
        db,
        actor_user_id=_current_user.user_id,
        role_id=role_id,
        tenant=tenant,
    )
    menu_ids = await role_service.get_role_menus(db, role_id, tenant=tenant)
    return ResponseModel.success(data=menu_ids)


@router.post(
    "/add",
    summary="创建新角色",
    description="创建新的角色，包括角色名称、编码和描述信息",
    responses={
        200: {"description": "创建成功"},
        400: {"description": "参数验证失败或角色编码已存在"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:role:add"))],
)
async def add_role(
    role_in: RoleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    创建新角色

    Args:
        role_in: 角色创建信息，包含角色名称、编码、描述和状态
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 创建成功的消息

    Request Body Fields:
        - role_name: 角色名称（必填，2-50字符）
        - role_code: 角色编码（必填，2-50字符，字母数字下划线，不能以数字开头）
        - role_desc: 角色描述（可选，最大200字符）
        - status: 角色状态（必填，1-启用，2-禁用）
    """
    await role_management_service.create(
        db,
        role_in,
        actor_user_id=current_user.user_id,
        tenant=tenant,
    )
    await db.commit()
    return ResponseModel.success(msg="角色创建成功")


@router.put(
    "/{role_id}",
    summary="编辑角色信息",
    dependencies=[Depends(require_permissions("system:role:edit"))],
)
async def update_role(
    role_id: int,
    role_in: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """更新角色基本信息"""
    await role_management_service.update(
        db,
        role_id,
        role_in,
        actor_user_id=current_user.user_id,
        tenant=tenant,
    )
    await db.commit()
    return ResponseModel.success(msg="角色更新成功")


@router.put(
    "/menu/{role_id}",
    summary="编辑角色菜单权限信息",
    dependencies=[Depends(require_permissions("system:role:menu-auth"))],
)
async def update_role_menu(
    role_id: int,
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """更新角色的菜单权限"""
    await role_management_service.update_menus(
        db,
        role_id,
        ids,
        actor_user_id=current_user.user_id,
        tenant=tenant,
    )
    await db.commit()
    return ResponseModel.success(msg="角色更新成功")


@router.delete(
    "/{role_id}",
    summary="删除指定角色",
    description="删除指定的单个角色",
    responses={
        200: {"description": "删除成功"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "角色不存在"},
    },
    dependencies=[Depends(require_permissions("system:role:delete"))],
)
async def delete_role(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    删除指定角色

    Args:
        role_id: 角色ID（路径参数）
        db: 异步数据库会话

    Returns:
        ResponseModel: 删除成功的消息

    Note:
        - 此操作不可逆，请谨慎操作
        - 角色删除后，关联的用户和菜单关系也会被清除
    """
    await role_service.delete_role(
        db,
        role_id,
        actor_user_id=current_user.user_id,
        tenant=tenant,
    )
    await db.commit()
    return ResponseModel.success(msg="角色删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除用户",
    dependencies=[Depends(require_permissions("system:role:batch-delete"))],
)
async def batch_delete_roles(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """批量删除角色"""
    deleted_count = await role_service.batch_delete_roles(
        db,
        ids,
        actor_user_id=current_user.user_id,
        tenant=tenant,
    )
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 条数据")


@router.get(
    "/{role_id}",
    response_model=ResponseModel[RoleOut],
    summary="获取角色详情",
    dependencies=[Depends(require_permissions("system:role:list"))],
)
async def get_role_detail(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """获取角色详情"""
    role = await role_management_service.authorize_role_projection(
        db,
        actor_user_id=current_user.user_id,
        role_id=role_id,
        tenant=tenant,
    )
    return ResponseModel.success(data=role)
