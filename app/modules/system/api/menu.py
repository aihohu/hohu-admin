from fastapi import APIRouter, Body, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import MENU_TYPE_BUTTON
from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from app.modules.system.schemas.menu import (
    MenuCreate,
    MenuOut,
    MenuQuery,
    MenuSimpleOut,
    MenuTreeOptionOut,
    MenuTreeOut,
    MenuUpdate,
)
from app.modules.system.service.menu_service import menu_service
from app.utils.pagination import paginate

router = APIRouter()


@router.get(
    "/tree",
    response_model=ResponseModel[list[MenuTreeOut]],
    summary="获取菜单树形列表",
)
async def get_menu_tree(db: AsyncSession = Depends(get_db)):
    """获取菜单树形列表（用于前端菜单管理页面）"""
    stmt = select(Menu).order_by(Menu.order.asc())
    result = await db.execute(stmt)
    menus = result.scalars().all()

    # 递归组装树形结构
    menu_map = {m.menu_id: MenuTreeOut.model_validate(m).model_dump() for m in menus}
    tree = []
    for _m_id, m_dict in menu_map.items():
        p_id = int(m_dict["parent_id"]) if m_dict["parent_id"] else None
        if p_id in menu_map:
            if "children" not in menu_map[p_id]:
                menu_map[p_id]["children"] = []
            menu_map[p_id]["children"].append(m_dict)
        else:
            tree.append(m_dict)

    return ResponseModel.success(data=tree)


@router.get(
    "/tree-option",
    response_model=ResponseModel[list[MenuTreeOptionOut]],
    summary="获取菜单树形列表(前端option结构)",
)
async def get_menu_tree_option(db: AsyncSession = Depends(get_db)):
    """获取菜单树形列表（用于前端下拉选择）"""
    stmt = select(Menu).where(Menu.status == "1").order_by(Menu.order.asc())
    result = await db.execute(stmt)
    menus = result.scalars().all()

    # 递归组装树形结构
    menu_map = {}
    for m in menus:
        menu_out = MenuTreeOptionOut(
            id=m.menu_id,
            label=m.menu_name,
            p_id=str(m.parent_id) if m.parent_id else "",
            children=[],
        )
        menu_map[m.menu_id] = menu_out

    # 构建树形结构
    tree = []
    for _menu_id, menu_out in menu_map.items():
        p_id = int(menu_out.p_id) if menu_out.p_id else None
        if p_id in menu_map:
            menu_map[p_id].children.append(menu_out)
        else:
            tree.append(menu_out)

    return ResponseModel.success(data=tree)


@router.get(
    "/tree-list",
    response_model=ResponseModel[PageResult[MenuTreeOut]],
    summary="获取菜单树形列表(带伪分页数据-适配前端)",
)
async def get_menu_tree_list(db: AsyncSession = Depends(get_db)):
    """获取菜单树形列表（带伪分页数据）"""
    stmt = select(Menu).order_by(Menu.order.asc())
    result = await db.execute(stmt)
    menus = result.scalars().all()

    # 预处理：将所有数据转为字典，并初始化 children 和 buttons
    menu_map = {}
    for m in menus:
        m_dict = MenuTreeOut.model_validate(m).model_dump()
        m_dict["children"] = []
        m_dict["buttons"] = []
        menu_map[m.menu_id] = m_dict

    tree = []

    # 第二次遍历：组装树形结构
    for m in menus:
        m_id = m.menu_id
        m_dict = menu_map[m_id]
        p_id = int(m.parent_id) if m.parent_id else None

        if p_id in menu_map:
            # 如果当前节点是按钮 (menu_type == 'F')
            if m.menu_type == MENU_TYPE_BUTTON:
                # 将按钮信息放入父节点的 buttons 中
                button_data = {
                    "desc": m.menu_name,
                    "code": m.permission,
                }
                menu_map[p_id]["buttons"].append(button_data)
            else:
                # 非按钮节点，放入父节点的 children 中
                menu_map[p_id]["children"].append(m_dict)
        else:
            # 没有父节点且不是按钮的作为根节点（通常 F 类不会是根节点）
            if m.menu_type != MENU_TYPE_BUTTON:
                tree.append(m_dict)

    page_data = PageResult(records=tree, total=len(tree), current=1, size=len(tree))
    return ResponseModel.success(data=page_data)


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[MenuOut]],
    summary="获取菜单分页列表",
)
async def list_menus(
    query: MenuQuery = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """获取菜单分页列表"""
    page_data = await paginate(
        db=db,
        model=Menu,
        query_params=query,
        order_by=Menu.order.asc(),
    )
    return ResponseModel.success(data=page_data)


@router.get(
    "/all",
    response_model=ResponseModel[list[MenuSimpleOut]],
    summary="获取全部菜单列表(不分页)",
)
async def get_all_menu(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取所有已启用的菜单列表"""
    menus = await menu_service.get_all_menus(db)
    return ResponseModel.success(data=menus)


@router.get(
    "/getAllPages",
    response_model=ResponseModel[list[str]],
    summary="获取所有页面",
)
async def get_all_pages(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取所有页面路由名称"""
    pages = await menu_service.get_all_pages(db)
    return ResponseModel.success(data=pages)


@router.post(
    "/add",
    summary="新增菜单",
)
async def add_menu(
    menu_in: MenuCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """新增菜单"""
    new_menu = await menu_service.create_menu(db, menu_in)
    new_menu.create_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="菜单创建成功")


@router.put(
    "/{menu_id}",
    summary="修改菜单",
)
async def update_menu(
    menu_id: int,
    menu_in: MenuUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """修改菜单"""
    menu = await menu_service.update_menu(db, menu_id, menu_in)
    menu.update_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="菜单更新成功")


@router.delete(
    "/{menu_id}",
    summary="删除单个菜单",
)
async def delete_menu(
    menu_id: int,
    db: AsyncSession = Depends(get_db),
):
    """删除单个菜单"""
    await menu_service.delete_menu(db, menu_id)
    await db.commit()
    return ResponseModel.success(msg="菜单删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除菜单",
)
async def batch_delete_menus(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """批量删除菜单"""
    deleted_count = await menu_service.batch_delete_menus(db, ids)
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 个菜单")
