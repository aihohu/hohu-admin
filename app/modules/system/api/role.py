from fastapi import APIRouter, Body, Depends
from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.core.exceptions import (
    CannotDeleteAdminException,
    DuplicateRoleException,
    InvalidParameterException,
    RoleNotFoundException,
)
from app.db.base import role_menus
from app.db.session import get_db
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.role import (
    RoleCreate,
    RoleOut,
    RoleQuery,
    RoleSimpleOut,
    RoleUpdate,
)
from app.utils.pagination import build_filters, paginate

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
    """
    支持根据角色名称、角色编码、状态进行模糊分页查询
    """
    # 构建查询条件
    field_mapping = {
        "role_name": ("role_name", "contains"),
        "role_code": ("role_code", "contains"),
        "status": ("status", "=="),
    }
    filters = build_filters(Role, field_mapping, **query.model_dump())

    # 使用通用分页查询
    page_data = await paginate(
        db=db,
        model=Role,
        query_params=query,
        filters=filters,
        order_by=Role.create_time.desc(),
    )

    return ResponseModel.success(data=page_data)


@router.get(
    "/all",
    response_model=ResponseModel[list[RoleSimpleOut]],
    summary="获取全部角色列表(不分页)",
)
async def get_all_roles(
    db: AsyncSession = Depends(get_db), _current_user: User = Depends(get_current_user)
):
    """
    获取系统中所有已启用的角色列表，常用于前端下拉选择框。
    # 仅限拥有 'sys:role:all' 权限或管理员访问。
    """
    # 只查询状态为 "1" (启用) 的角色，按创建时间排序
    stmt = (
        select(Role)
        .where(Role.status == STATUS_ENABLED)
        .order_by(Role.create_time.asc())
    )
    result = await db.execute(stmt)
    roles = result.scalars().all()

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
    # 子查询：找出该角色拥有的菜单中，作为 parent_id 出现过的 ID
    subquery = (
        select(Menu.parent_id)
        .join(role_menus, Menu.menu_id == role_menus.c.menu_id)
        .where(role_menus.c.role_id == role_id)
        .scalar_subquery()  # 转换为标量子查询
    )

    # 主查询
    stmt = (
        select(Menu.menu_id)
        .outerjoin(role_menus, Menu.menu_id == role_menus.c.menu_id)
        .where(and_(role_menus.c.role_id == role_id, Menu.menu_id.not_in(subquery)))
        .order_by(Menu.parent_id, Menu.order)
    )

    result = await db.execute(stmt)
    menu_ids = [str(menu_id) for menu_id in result.scalars().all()]

    return ResponseModel.success(data=menu_ids)


@router.post("/add", summary="创建新角色")
async def add_role(
    role_in: RoleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    创建角色，并自动记录创建人
    """
    # 检查编码唯一性
    check = await db.execute(select(Role).where(Role.role_code == role_in.role_code))
    if check.scalars().first():
        raise DuplicateRoleException(role_in.role_code)

    new_role = Role(**role_in.model_dump(), create_by=current_user.user_name)
    db.add(new_role)
    await db.commit()
    return ResponseModel.success(msg="角色创建成功")


@router.put("/{role_id}", summary="编辑角色信息")
async def update_role(
    role_id: int,
    role_in: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    根据 ID 更新角色基本信息，并自动更新修改人
    """
    role = await db.get(Role, role_id)
    if not role:
        raise RoleNotFoundException()

    update_data = role_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(role, field, value)

    role.update_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="角色更新成功")


@router.put("/menu/{role_id}", summary="编辑角色菜单权限信息")
async def update_role_menu(
    role_id: int,
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    根据 ID 更新角色 菜单权限，并自动更新修改人
    """
    role = await db.get(Role, role_id)
    if not role:
        raise RoleNotFoundException()

    if ids:
        menu_result = await db.execute(select(Menu).where(Menu.menu_id.in_(ids)))
        role.menus = menu_result.scalars().all()

    role.update_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="角色更新成功")


@router.delete("/{role_id}", summary="删除指定角色")
async def delete_role(role_id: int, db: AsyncSession = Depends(get_db)):
    """
    物理删除角色。注意：在有用户关联此角色时应谨慎操作
    """
    role = await db.get(Role, role_id)
    if not role:
        raise RoleNotFoundException()

    await db.delete(role)
    await db.commit()
    return ResponseModel.success(msg="角色删除成功")


@router.post("/batch-delete", summary="批量删除用户")
async def batch_delete_roles(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """批量删除角色"""
    if not ids:
        raise InvalidParameterException("未选择要删除的角色")
    # 过滤掉 超级管理员 权限，防止误删
    check_stmt = select(Role.role_id).where(
        and_(Role.role_id.in_(ids), Role.role_code == SUPER_ADMIN_ROLE_CODE)
    )
    admin_result = await db.execute(check_stmt)
    if admin_result.scalars().first():
        raise CannotDeleteAdminException("系统管理员角色")

    stmt = delete(Role).where(Role.role_id.in_(ids))
    result = await db.execute(stmt)

    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {result.rowcount} 条数据")


@router.get("/{role_id}", response_model=ResponseModel[RoleOut], summary="获取角色详情")
async def get_role_detail(role_id: int, db: AsyncSession = Depends(get_db)):
    """
    根据 ID 获取单个角色的完整信息
    """
    role = await db.get(Role, role_id)
    if not role:
        raise RoleNotFoundException()
    return ResponseModel.success(data=role)
