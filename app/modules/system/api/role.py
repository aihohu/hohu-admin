from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import ResponseModel
from app.core.tenant import TenantContext
from app.db.authorization_http import relation_read, system_write
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context
from app.modules.system.models.user import User
from app.modules.system.schemas.relation_form import RelationFormOut, RelationFormQuery
from app.modules.system.schemas.relation_policy import RelationPage
from app.modules.system.schemas.relation_projection import (
    ProjectedRoleOut,
    ProjectedRoleSummaryOut,
)
from app.modules.system.schemas.role import (
    RoleCreate,
    RoleQuery,
    RoleUpdate,
)

router = APIRouter()


@router.get(
    "/form-context",
    response_model=ResponseModel[RelationFormOut[ProjectedRoleOut]],
    summary="获取当前表单与字段能力",
)
async def get_form_context(
    query: RelationFormQuery = Query(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="role.form", query=query, tenant=tenant
        )
    )


@router.get(
    "/list",
    response_model=ResponseModel[RelationPage[ProjectedRoleSummaryOut]],
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
    return ResponseModel.success(
        data=await relation_read(
            db, _current_user, operation="role.list", tenant=tenant, query=query
        )
    )


@router.get(
    "/all",
    response_model=ResponseModel[list[ProjectedRoleSummaryOut]],
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
    return ResponseModel.success(
        data=await relation_read(db, _current_user, operation="role.all", tenant=tenant)
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
    return ResponseModel.success(
        data=await relation_read(
            db, _current_user, operation="role.menus", tenant=tenant, target_id=role_id
        )
    )


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
    await system_write(
        db, current_user, operation="role.create", payload=role_in, tenant=tenant
    )
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
    await system_write(
        db,
        current_user,
        operation="role.update",
        payload=role_in,
        target_id=role_id,
        tenant=tenant,
    )
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
    await system_write(
        db,
        current_user,
        operation="role.menus",
        payload=ids,
        target_id=role_id,
        tenant=tenant,
    )
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
    await system_write(
        db, current_user, operation="role.delete", target_id=role_id, tenant=tenant
    )
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
    outcome = await system_write(
        db, current_user, operation="role.batch_delete", payload=ids, tenant=tenant
    )
    deleted_count = outcome["result"]
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 条数据")


@router.get(
    "/{role_id}",
    response_model=ResponseModel[ProjectedRoleOut],
    summary="获取角色详情",
    dependencies=[Depends(require_permissions("system:role:list"))],
)
async def get_role_detail(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="role.detail", tenant=tenant, target_id=role_id
        )
    )
