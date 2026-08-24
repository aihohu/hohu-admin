from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.dept import Dept
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import (
    DeptCreate,
    DeptMove,
    DeptOut,
    DeptQuery,
    DeptTreeOptionOut,
    DeptTreeOut,
    DeptUpdate,
    DeptUsersOut,
    DeptUsersUpdate,
)
from app.modules.system.service.dept_selector import department_selector
from app.modules.system.service.dept_service import dept_service
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service,
)
from app.utils.data_scope import resolve_data_scope
from app.utils.pagination import build_filters

router = APIRouter()


def _dept_filters(query: DeptQuery) -> list:
    """Build the shared department metadata filters."""
    return build_filters(
        Dept,
        {
            "dept_name": ("dept_name", "contains"),
            "status": ("status", "=="),
            "leader": ("leader", "contains"),
        },
        **query.model_dump(),
    )


def _build_dept_tree(depts: list[Dept]) -> list[dict]:
    """Project visible rows as a local-root tree."""
    dept_by_id = {int(dept.dept_id): dept for dept in depts}
    visible_ids = set(dept_by_id)
    local_ancestors: dict[int, str] = {}

    def ancestors_for(dept: Dept, visiting: frozenset[int] = frozenset()) -> str:
        dept_id = int(dept.dept_id)
        if dept_id in local_ancestors:
            return local_ancestors[dept_id]
        parent_id = int(dept.parent_id) if dept.parent_id is not None else None
        if parent_id not in visible_ids or dept_id in visiting:
            local_ancestors[dept_id] = "0"
            return "0"
        parent = dept_by_id[parent_id]
        value = f"{ancestors_for(parent, visiting | {dept_id})},{parent_id}"
        local_ancestors[dept_id] = value
        return value

    dept_map: dict[int, dict] = {}
    for dept in depts:
        item = DeptTreeOut.model_validate(dept).model_dump()
        parent_id = int(dept.parent_id) if dept.parent_id is not None else None
        item["parent_id"] = str(parent_id) if parent_id in visible_ids else None
        item["ancestors"] = ancestors_for(dept)
        item["children"] = []
        dept_map[int(dept.dept_id)] = item

    tree: list[dict] = []
    for dept in depts:
        item = dept_map[int(dept.dept_id)]
        parent_id = int(dept.parent_id) if dept.parent_id is not None else None
        if parent_id in dept_map:
            dept_map[parent_id]["children"].append(item)
        else:
            tree.append(item)
    return tree


def _build_dept_options(depts: list[Dept]) -> list[DeptTreeOptionOut]:
    """Project visible enabled rows as local-root options."""
    visible_ids = {int(dept.dept_id) for dept in depts}
    dept_map = {
        int(dept.dept_id): DeptTreeOptionOut(
            id=dept.dept_id,
            label=dept.dept_name,
            p_id=(
                str(dept.parent_id)
                if dept.parent_id is not None and int(dept.parent_id) in visible_ids
                else ""
            ),
            children=[],
        )
        for dept in depts
    }
    tree: list[DeptTreeOptionOut] = []
    for dept in depts:
        option = dept_map[int(dept.dept_id)]
        parent_id = int(dept.parent_id) if dept.parent_id is not None else None
        if parent_id in dept_map:
            dept_map[parent_id].children.append(option)
        else:
            tree.append(option)
    return tree


@router.get(
    "/tree",
    response_model=ResponseModel[list[DeptTreeOut]],
    summary="获取部门树形结构",
    description="获取所有部门的树形结构，用于部门管理页面",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_tree(
    query: DeptQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取部门树形结构（支持筛选）"""
    scope = await resolve_data_scope(db, current_user)
    depts = await department_selector.rows(
        db,
        scope=scope,
        filters=_dept_filters(query),
    )
    return ResponseModel.success(data=_build_dept_tree(depts))


@router.get(
    "/tree-option",
    response_model=ResponseModel[list[DeptTreeOptionOut]],
    summary="获取部门下拉选项",
    description="获取启用状态的部门树形选项，用于下拉选择",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_tree_option(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取部门下拉选项（仅启用状态）"""
    scope = await resolve_data_scope(db, current_user)
    depts = await department_selector.rows(
        db,
        scope=scope,
        filters=[Dept.status == "1"],
    )
    return ResponseModel.success(data=_build_dept_options(depts))


@router.get(
    "/tree-list",
    response_model=ResponseModel[PageResult[DeptTreeOut]],
    summary="获取部门树形列表(带伪分页)",
    description="获取部门树形结构，包装为 PageResult 适配前端",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_tree_list(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取部门树形列表（带伪分页）"""
    scope = await resolve_data_scope(db, current_user)
    depts = await department_selector.rows(db, scope=scope)
    tree = _build_dept_tree(depts)
    page_data = PageResult(records=tree, total=len(tree), current=1, size=len(tree))
    return ResponseModel.success(data=page_data)


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[DeptOut]],
    summary="获取部门分页列表",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_list(
    query: DeptQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取部门分页列表"""
    scope = await resolve_data_scope(db, current_user)
    page_data = await department_selector.page(
        db,
        scope=scope,
        current=query.current,
        size=query.size,
        filters=_dept_filters(query),
    )
    return ResponseModel.success(data=page_data)


@router.get(
    "/{dept_id}",
    response_model=ResponseModel[DeptOut],
    summary="获取部门详情",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_detail(
    dept_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取部门详情"""
    scope = await resolve_data_scope(db, current_user)
    dept = await department_selector.get_by_id(db, scope=scope, dept_id=dept_id)
    return ResponseModel.success(data=dept)


@router.post(
    "/add",
    summary="创建部门",
    description="创建新的部门",
    dependencies=[
        Depends(require_permissions("system:dept:add")),
        Depends(require_permissions("system:dept:list")),
    ],
)
async def add_dept(
    dept_in: DeptCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建部门"""
    new_dept = await dept_service.create(
        db,
        dept_in,
        actor_user_id=current_user.user_id,
    )
    new_dept.create_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="部门创建成功")


@router.put(
    "/{dept_id}",
    summary="更新部门",
    description="更新指定部门信息",
    dependencies=[
        Depends(require_permissions("system:dept:edit")),
        Depends(require_permissions("system:dept:list")),
    ],
)
async def update_dept(
    dept_id: int,
    dept_in: DeptUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新部门"""
    dept = await dept_service.update(
        db,
        dept_id,
        dept_in,
        actor_user_id=current_user.user_id,
    )
    dept.update_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="部门更新成功")


@router.put(
    "/{dept_id}/move",
    summary="移动部门",
    description="通过独立授权策略移动部门子树",
    dependencies=[
        Depends(require_permissions("system:dept:move")),
        Depends(require_permissions("system:dept:list")),
    ],
)
async def move_dept(
    dept_id: int,
    move_in: DeptMove,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move one department subtree through the shared write policy."""
    dept = await dept_service.move(
        db,
        dept_id=dept_id,
        new_parent_id=move_in.new_parent_id,
        actor_user_id=current_user.user_id,
    )
    dept.update_by = current_user.user_name
    await db.commit()
    return ResponseModel.success(msg="部门移动成功")


@router.delete(
    "/{dept_id}",
    summary="删除部门",
    description="删除指定部门",
    dependencies=[Depends(require_permissions("system:dept:delete"))],
)
async def delete_dept(
    dept_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除部门"""
    await dept_service.delete(
        db,
        dept_id,
        actor_user_id=current_user.user_id,
    )
    await db.commit()
    return ResponseModel.success(msg="部门删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除部门",
    description="批量删除多个部门",
    dependencies=[Depends(require_permissions("system:dept:batch-delete"))],
)
async def batch_delete_depts(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量删除部门"""
    deleted_count = await dept_service.batch_delete(
        db,
        ids,
        actor_user_id=current_user.user_id,
    )
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 个部门")


@router.get(
    "/{dept_id}/users",
    response_model=ResponseModel[DeptUsersOut],
    summary="获取部门用户管理数据",
    description="返回当前用户数据范围内的部门成员候选分页",
    dependencies=[
        Depends(require_permissions("system:dept:list")),
        Depends(require_permissions("system:dept:edit")),
        Depends(require_permissions("system:user:list")),
    ],
)
async def get_dept_users(
    dept_id: int,
    query: str | None = Query(None, max_length=100),
    current: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return one user-scoped page of membership candidates."""
    page = await user_department_assignment_service.list_department_members(
        db,
        actor_user_id=current_user.user_id,
        dept_id=dept_id,
        query=query,
        current=current,
        size=size,
    )
    return ResponseModel.success(data=DeptUsersOut.model_validate(page))


@router.put(
    "/{dept_id}/users",
    summary="批量更新部门用户",
    description="传入最终成员用户ID列表，后端 diff 出新增/移除并批量更新",
    dependencies=[
        Depends(require_permissions("system:dept:list")),
        Depends(require_permissions("system:dept:edit")),
        Depends(require_permissions("system:user:edit")),
    ],
)
async def update_dept_users(
    dept_id: int,
    body: DeptUsersUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Replace the complete department member set atomically."""
    result = await user_department_assignment_service.replace_department_members(
        db,
        actor_user_id=current_user.user_id,
        dept_id=dept_id,
        user_ids=body.user_ids,
    )
    await db.commit()
    return ResponseModel.success(
        msg=f"新增 {result.added} 人，移除 {result.removed} 人"
    )
