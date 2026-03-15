from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.role import (
    RoleCreate,
    RoleOut,
    RoleQuery,
    RoleSimpleOut,
    RoleUpdate,
)
from app.modules.system.service.role_service import role_service

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[RoleOut]],
    summary="获取角色列表分页",
)
async def list_roles(
    query: RoleQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取角色分页列表"""
    page_data = await role_service.get_role_list(db, query)
    return ResponseModel.success(data=page_data)


@router.get(
    "/all",
    response_model=ResponseModel[list[RoleSimpleOut]],
    summary="获取全部角色列表(不分页)",
)
async def get_all_roles(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取所有已启用的角色列表"""
    roles = await role_service.get_all_roles(db)
    return ResponseModel.success(data=roles)


@router.get(
    "/menus/{role_id}",
    response_model=ResponseModel[list[str]],
    summary="获取角色菜单列表",
)
async def get_menus(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取角色的菜单ID列表"""
    menu_ids = await role_service.get_role_menus(db, role_id)
    return ResponseModel.success(data=menu_ids)


@router.post("/add", summary="创建新角色")
async def add_role(
    role_in: RoleCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """创建新角色"""
    await role_service.create_role(db, role_in)
    await db.commit()
    return ResponseModel.success(msg="角色创建成功")


@router.put("/{role_id}", summary="编辑角色信息")
async def update_role(
    role_id: int,
    role_in: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """更新角色基本信息"""
    await role_service.update_role(db, role_id, role_in)
    await db.commit()
    return ResponseModel.success(msg="角色更新成功")


@router.put("/menu/{role_id}", summary="编辑角色菜单权限信息")
async def update_role_menu(
    role_id: int,
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """更新角色的菜单权限"""
    await role_service.update_role_menu(db, role_id, ids)
    await db.commit()
    return ResponseModel.success(msg="角色更新成功")


@router.delete("/{role_id}", summary="删除指定角色")
async def delete_role(
    role_id: int,
    db: AsyncSession = Depends(get_db),
):
    """删除角色"""
    await role_service.delete_role(db, role_id)
    await db.commit()
    return ResponseModel.success(msg="角色删除成功")


@router.post("/batch-delete", summary="批量删除用户")
async def batch_delete_roles(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """批量删除角色"""
    deleted_count = await role_service.batch_delete_roles(db, ids)
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 条数据")


@router.get("/{role_id}", response_model=ResponseModel[RoleOut], summary="获取角色详情")
async def get_role_detail(
    role_id: int,
    db: AsyncSession = Depends(get_db),
):
    """获取角色详情"""
    role = await role_service.get_role_detail(db, role_id)
    return ResponseModel.success(data=role)
