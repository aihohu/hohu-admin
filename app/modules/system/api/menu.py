from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

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

router = APIRouter()


# 树形列表 (通常用于前端菜单管理页面)
@router.get(
    "/tree", response_model=ResponseModel[list[MenuTreeOut]], summary="获取菜单树形列表"
)
async def get_menu_tree(db: AsyncSession = Depends(get_db)):
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
    return ResponseModel.success(data=tree)  # 1. 树形列表 (通常用于前端菜单管理页面)


@router.get(
    "/tree-option",
    response_model=ResponseModel[list[MenuTreeOptionOut]],
    summary="获取菜单树形列表(前端option结构)",
)
async def get_menu_tree_option(db: AsyncSession = Depends(get_db)):
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


# 树形列表 (通常用于前端菜单管理页面)
@router.get(
    "/tree-list",
    response_model=ResponseModel[PageResult[MenuTreeOut]],
    summary="获取菜单树形列表(带伪分页数据-适配前端)",
)
async def get_menu_tree_list(db: AsyncSession = Depends(get_db)):
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
            if m.menu_type == "F":
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
            if m.menu_type != "F":
                tree.append(m_dict)

    page_data = PageResult(records=tree, total=len(tree), current=1, size=len(tree))
    return ResponseModel.success(data=page_data)


# 分页列表 (备用，某些简单管理页面使用)
@router.get(
    "/list",
    response_model=ResponseModel[PageResult[MenuOut]],
    summary="获取菜单分页列表",
)
async def list_menus(query: MenuQuery = Depends(), db: AsyncSession = Depends(get_db)):
    count_stmt = select(func.count()).select_from(Menu)
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = (
        select(Menu)
        .offset((query.current - 1) * query.size)
        .limit(query.size)
        .order_by(Menu.order.asc())
    )
    result = await db.execute(stmt)
    return ResponseModel.success(
        data=PageResult(
            records=result.scalars().all(),
            total=total,
            current=query.current,
            size=query.size,
        )
    )


@router.get(
    "/all",
    response_model=ResponseModel[list[MenuSimpleOut]],
    summary="获取全部菜单列表(不分页)",
)
async def get_all_menu(
    db: AsyncSession = Depends(get_db), _current_user: User = Depends(get_current_user)
):
    # 只查询状态为 "1" (启用) 的菜单，按创建时间排序
    stmt = select(Menu).where(Menu.status == "1").order_by(Menu.order.asc())
    result = await db.execute(stmt)
    menus = result.scalars().all()

    return ResponseModel.success(data=menus)


@router.get(
    "/getAllPages",
    response_model=ResponseModel[list[str]],
    summary="获取所有页面",
)
async def get_all_pages(
    db: AsyncSession = Depends(get_db), _current_user: User = Depends(get_current_user)
):
    # 只查询状态为 "1" (启用) 的菜单，按创建时间排序
    stmt = (
        select(Menu.route_name)
        .where(Menu.status == "1", Menu.menu_type == "C")
        .order_by(Menu.order.asc())
    )
    result = await db.execute(stmt)
    menus = result.scalars().all()

    return ResponseModel.success(data=menus)


# 新增菜单
@router.post(
    "/add",
    summary="新增菜单",
    # dependencies=[Depends(require_permissions("sys:menu:add"))],
)
async def add_menu(
    menu_in: MenuCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    new_menu = Menu(**menu_in.model_dump(), create_by=current_user.user_name)
    db.add(new_menu)
    await db.commit()
    return ResponseModel.success(msg="菜单创建成功")


# 更新菜单
@router.put(
    "/{menu_id}",
    summary="修改菜单",
    # dependencies=[Depends(require_permissions("sys:menu:update"))],
)
async def update_menu(
    menu_id: int,
    menu_in: MenuUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    menu = await db.get(Menu, menu_id)
    if not menu:
        raise HTTPException(status_code=404, detail="菜单不存在")

    update_data = menu_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(menu, field, value)

    # 更新按钮权限
    if menu_in.buttons is not None:
        # 删除该菜单下所有现有按钮（只删 button 类型）
        delete_stmt = delete(Menu).where(
            Menu.parent_id == menu_id, Menu.menu_type == "F"
        )
        await db.execute(delete_stmt)

        # 批量添加新按钮
        new_buttons = []
        for btn in menu_in.buttons:
            button_menu = Menu(
                menu_name=btn.desc,
                permission=btn.code,
                menu_type="F",
                parent_id=menu_id,
                create_by=current_user.user_name,
                update_by=current_user.user_name,
                order=0,
                status="1",
            )
            new_buttons.append(button_menu)

        db.add_all(new_buttons)

    menu.update_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="菜单更新成功")


# 删除单个菜单
@router.delete(
    "/{menu_id}",
    summary="删除单个菜单",
    # dependencies=[Depends(require_permissions("sys:menu:delete"))],
)
async def delete_menu(menu_id: int, db: AsyncSession = Depends(get_db)):
    # 检查是否有子菜单
    child_stmt = select(Menu).where(Menu.parent_id == menu_id)
    child = (await db.execute(child_stmt)).first()
    if child:
        raise HTTPException(status_code=400, detail="请先删除子菜单")

    menu = await db.get(Menu, menu_id)
    if not menu:
        raise HTTPException(status_code=404, detail="菜单不存在")

    await db.delete(menu)
    await db.commit()
    return ResponseModel.success(msg="菜单删除成功")


# 批量删除菜单
@router.post(
    "/batch-delete",
    summary="批量删除菜单",
    # dependencies=[Depends(require_permissions("sys:menu:delete"))],
)
async def batch_delete_menus(
    ids: list[int] = Body(...), db: AsyncSession = Depends(get_db)
):
    if not ids:
        return ResponseModel.error(msg="请选择要删除的菜单")

    # 批量检查子菜单逻辑 (简单处理：如果选中的菜单中有任何一个包含不在选中列表里的子菜单，则禁止)
    check_stmt = select(Menu).where(
        and_(Menu.parent_id.in_(ids), ~Menu.menu_id.in_(ids))
    )
    has_child = (await db.execute(check_stmt)).first()
    if has_child:
        raise HTTPException(
            status_code=400, detail="选中的菜单中包含未选中的子菜单，请先处理"
        )

    stmt = delete(Menu).where(Menu.menu_id.in_(ids))
    result = await db.execute(stmt)
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {result.rowcount} 个菜单")
