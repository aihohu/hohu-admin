from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import TenantContext
from app.db.authorization_http import control_write, relation_read
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context
from app.modules.system.models.user import User
from app.modules.system.schemas.menu import (
    MenuCreate,
    MenuQuery,
    MenuUpdate,
)
from app.modules.system.schemas.relation_projection import (
    ProjectedMenuOut,
    ProjectedMenuSimpleOut,
    ProjectedTreeOptionOut,
)

router = APIRouter()


@router.get(
    "/tree",
    response_model=ResponseModel[list[ProjectedMenuOut]],
    summary="获取菜单树形列表",
    description="获取所有菜单的树形结构，用于前端菜单管理页面",
    responses={
        200: {"description": "获取成功，返回菜单树形结构"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:menu:list"))],
)
async def get_menu_tree(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(db, current_user, operation="menu.tree", tenant=tenant)
    )


@router.get(
    "/tree-option",
    response_model=ResponseModel[list[ProjectedTreeOptionOut]],
    summary="获取菜单树形列表(前端option结构)",
)
async def get_menu_tree_option(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="menu.options", tenant=tenant
        )
    )


@router.get(
    "/tree-list",
    response_model=ResponseModel[PageResult[ProjectedMenuOut]],
    summary="获取菜单树形列表(带伪分页数据-适配前端)",
    dependencies=[Depends(require_permissions("system:menu:list"))],
)
async def get_menu_tree_list(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="menu.tree-list", tenant=tenant
        )
    )


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[ProjectedMenuOut]],
    summary="获取菜单分页列表",
    dependencies=[Depends(require_permissions("system:menu:list"))],
)
async def list_menus(
    query: MenuQuery = Depends(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="menu.list", tenant=tenant, query=query
        )
    )


@router.get(
    "/all",
    response_model=ResponseModel[list[ProjectedMenuSimpleOut]],
    summary="获取全部菜单列表(不分页)",
)
async def get_all_menu(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(db, _current_user, operation="menu.all", tenant=tenant)
    )


@router.get(
    "/getAllPages",
    response_model=ResponseModel[list[str]],
    summary="获取所有页面",
)
async def get_all_pages(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, _current_user, operation="menu.pages", tenant=tenant
        )
    )


@router.post(
    "/add",
    summary="新增菜单",
    description="创建新的菜单，支持目录、菜单和按钮三种类型",
    responses={
        200: {"description": "创建成功"},
        400: {"description": "参数验证失败"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:menu:add"))],
)
async def add_menu(
    menu_in: MenuCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    新增菜单

    Args:
        menu_in: 菜单创建信息，包含菜单名称、类型、路由等所有配置
        db: 异步数据库会话
        current_user: 当前登录用户对象

    Returns:
        ResponseModel: 创建成功的消息

    Request Body Fields:
        - parent_id: 父菜单ID（可选，0表示根菜单）
        - menu_name: 菜单名称（必填，最大50字符）
        - menu_type: 菜单类型（必填，M-目录，C-菜单，F-按钮）
        - route_name: 路由名称（可选）
        - route_path: 路由路径（可选）
        - order: 排序值（必填，默认0）
        - status: 菜单状态（必填，1-启用，2-禁用）
        - buttons: 按钮列表（可选，最多20个）
    """
    await control_write(
        db, current_user, operation="menu.create", payload=menu_in, tenant=tenant
    )
    return ResponseModel.success(msg="菜单创建成功")


@router.put(
    "/{menu_id}",
    summary="修改菜单",
    description="更新指定菜单的信息",
    responses={
        200: {"description": "更新成功"},
        400: {"description": "参数验证失败"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "菜单不存在"},
    },
    dependencies=[Depends(require_permissions("system:menu:edit"))],
)
async def update_menu(
    menu_id: int,
    menu_in: MenuUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    修改菜单

    Args:
        menu_id: 菜单ID（路径参数）
        menu_in: 菜单更新信息，所有字段都是可选的
        db: 异步数据库会话
        current_user: 当前登录用户对象

    Returns:
        ResponseModel: 更新成功的消息

    Path Parameters:
        - menu_id: 要更新的菜单ID

    Request Body Fields (Optional):
        - menu_name: 菜单名称
        - menu_type: 菜单类型
        - route_name: 路由名称
        - order: 排序值
        - status: 菜单状态
        - buttons: 按钮列表
    """
    await control_write(
        db,
        current_user,
        operation="menu.update",
        payload=menu_in,
        target_id=menu_id,
        tenant=tenant,
    )
    return ResponseModel.success(msg="菜单更新成功")


@router.delete(
    "/{menu_id}",
    summary="删除单个菜单",
    description="删除指定的单个菜单",
    responses={
        200: {"description": "删除成功"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "菜单不存在"},
    },
    dependencies=[Depends(require_permissions("system:menu:delete"))],
)
async def delete_menu(
    menu_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
    current_user: User = Depends(get_current_user),
):
    """
    删除单个菜单

    Args:
        menu_id: 菜单ID（路径参数）
        db: 异步数据库会话

    Returns:
        ResponseModel: 删除成功的消息

    Path Parameters:
        - menu_id: 要删除的菜单ID

    Note:
        - 此操作不可逆，请谨慎操作
        - 如果删除的是目录，其子菜单也会被删除
        - 菜单删除后，关联的角色关系也会被清除
    """
    await control_write(
        db, current_user, operation="menu.delete", target_id=menu_id, tenant=tenant
    )
    return ResponseModel.success(msg="菜单删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除菜单",
    description="批量删除多个菜单，支持传入菜单ID列表",
    responses={
        200: {"description": "删除成功"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:menu:batch-delete"))],
)
async def batch_delete_menus(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
    current_user: User = Depends(get_current_user),
):
    """
    批量删除菜单

    Args:
        ids: 菜单ID列表
        db: 异步数据库会话

    Returns:
        ResponseModel: 删除成功的消息，包含实际删除的菜单数量

    Request Body:
        - ids: 菜单ID数组（如：[123456, 123457, 123458]）

    Note:
        - 此操作不可逆，请谨慎操作
        - 如果删除的菜单是目录，其子菜单也会被删除
        - 菜单删除后，关联的角色关系也会被清除
    """
    deleted_count = await control_write(
        db, current_user, operation="menu.batch_delete", payload=ids, tenant=tenant
    )
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 个菜单")
