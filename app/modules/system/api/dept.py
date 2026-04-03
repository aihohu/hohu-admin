from fastapi import APIRouter, Body, Depends
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.dept import Dept
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import (
    DeptCreate,
    DeptOut,
    DeptQuery,
    DeptTreeOptionOut,
    DeptTreeOut,
    DeptUpdate,
)
from app.modules.system.service.dept_service import dept_service
from app.utils.pagination import build_filters, paginate

router = APIRouter()


@router.get(
    "/tree",
    response_model=ResponseModel[list[DeptTreeOut]],
    summary="获取部门树形结构",
    description="获取所有部门的树形结构，用于部门管理页面",
)
async def get_dept_tree(
    query: DeptQuery = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """获取部门树形结构（支持筛选）"""
    stmt = select(Dept).order_by(Dept.order_num.asc())

    # 构建筛选条件
    field_mapping = {
        "dept_name": ("dept_name", "contains"),
        "status": ("status", "=="),
        "leader": ("leader", "contains"),
    }
    filters = build_filters(Dept, field_mapping, **query.model_dump())
    if filters:
        stmt = stmt.where(and_(*filters))

    result = await db.execute(stmt)
    depts = result.scalars().all()

    # 构建映射
    dept_map = {d.dept_id: DeptTreeOut.model_validate(d).model_dump() for d in depts}

    # 组装树
    tree = []
    for _d_id, d_dict in dept_map.items():
        p_id = int(d_dict["parent_id"]) if d_dict["parent_id"] else None
        if p_id in dept_map:
            dept_map[p_id].setdefault("children", []).append(d_dict)
        else:
            tree.append(d_dict)

    return ResponseModel.success(data=tree)


@router.get(
    "/tree-option",
    response_model=ResponseModel[list[DeptTreeOptionOut]],
    summary="获取部门下拉选项",
    description="获取启用状态的部门树形选项，用于下拉选择",
)
async def get_dept_tree_option(db: AsyncSession = Depends(get_db)):
    """获取部门下拉选项（仅启用状态）"""
    stmt = select(Dept).where(Dept.status == "1").order_by(Dept.order_num.asc())
    result = await db.execute(stmt)
    depts = result.scalars().all()

    # 构建映射
    dept_map = {}
    for d in depts:
        dept_out = DeptTreeOptionOut(
            id=d.dept_id,
            label=d.dept_name,
            p_id=str(d.parent_id) if d.parent_id else "",
            children=[],
        )
        dept_map[d.dept_id] = dept_out

    # 构建树
    tree = []
    for _dept_id, dept_out in dept_map.items():
        p_id = int(dept_out.p_id) if dept_out.p_id else None
        if p_id in dept_map:
            dept_map[p_id].children.append(dept_out)
        else:
            tree.append(dept_out)

    return ResponseModel.success(data=tree)


@router.get(
    "/tree-list",
    response_model=ResponseModel[PageResult[DeptTreeOut]],
    summary="获取部门树形列表(带伪分页)",
    description="获取部门树形结构，包装为 PageResult 适配前端",
)
async def get_dept_tree_list(db: AsyncSession = Depends(get_db)):
    """获取部门树形列表（带伪分页）"""
    stmt = select(Dept).order_by(Dept.order_num.asc())
    result = await db.execute(stmt)
    depts = result.scalars().all()

    # 预处理
    dept_map = {}
    for d in depts:
        d_dict = DeptTreeOut.model_validate(d).model_dump()
        d_dict["children"] = []
        dept_map[d.dept_id] = d_dict

    tree = []
    for d in depts:
        d_dict = dept_map[d.dept_id]
        p_id = int(d.parent_id) if d.parent_id else None

        if p_id in dept_map:
            dept_map[p_id]["children"].append(d_dict)
        else:
            tree.append(d_dict)

    page_data = PageResult(records=tree, total=len(tree), current=1, size=len(tree))
    return ResponseModel.success(data=page_data)


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[DeptOut]],
    summary="获取部门分页列表",
)
async def get_dept_list(
    query: DeptQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取部门分页列表"""
    field_mapping = {
        "dept_name": ("dept_name", "contains"),
        "status": ("status", "=="),
        "leader": ("leader", "contains"),
    }
    from app.utils.pagination import build_filters

    filters = build_filters(Dept, field_mapping, **query.model_dump())
    page_data = await paginate(
        db=db,
        model=Dept,
        query_params=query,
        filters=filters,
        order_by=Dept.order_num.asc(),
    )
    return ResponseModel.success(data=page_data)


@router.get(
    "/{dept_id}",
    response_model=ResponseModel[DeptOut],
    summary="获取部门详情",
)
async def get_dept_detail(
    dept_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取部门详情"""
    dept = await dept_service.get_by_id(db, dept_id)
    return ResponseModel.success(data=dept)


@router.post(
    "/add",
    summary="创建部门",
    description="创建新的部门",
)
async def add_dept(
    dept_in: DeptCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建部门"""
    new_dept = await dept_service.create(db, dept_in)
    new_dept.create_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="部门创建成功")


@router.put(
    "/{dept_id}",
    summary="更新部门",
    description="更新指定部门信息",
)
async def update_dept(
    dept_id: int,
    dept_in: DeptUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新部门"""
    dept = await dept_service.update(db, dept_id, dept_in)
    dept.update_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="部门更新成功")


@router.delete(
    "/{dept_id}",
    summary="删除部门",
    description="删除指定部门",
)
async def delete_dept(
    dept_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """删除部门"""
    await dept_service.delete(db, dept_id)
    await db.commit()
    return ResponseModel.success(msg="部门删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除部门",
    description="批量删除多个部门",
)
async def batch_delete_depts(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """批量删除部门"""
    deleted_count = await dept_service.batch_delete(db, ids)
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 个部门")
