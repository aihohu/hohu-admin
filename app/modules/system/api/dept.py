from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import TenantContext
from app.db.authorization_http import relation_read, system_write, user_read
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import (
    DeptCreate,
    DeptMove,
    DeptQuery,
    DeptUpdate,
    DeptUsersUpdate,
)
from app.modules.system.schemas.relation_projection import (
    ProjectedDeptOut,
    ProjectedTreeOptionOut,
)
from app.modules.system.schemas.user_projection import ProjectedUserOut, UserMemberQuery

router = APIRouter()


@router.get(
    "/tree",
    response_model=ResponseModel[list[ProjectedDeptOut]],
    summary="获取部门树形结构",
    description="获取所有部门的树形结构，用于部门管理页面",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_tree(
    query: DeptQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="dept.tree", tenant=tenant, query=query
        )
    )


@router.get(
    "/tree-option",
    response_model=ResponseModel[list[ProjectedTreeOptionOut]],
    summary="获取部门下拉选项",
    description="获取启用状态的部门树形选项，用于下拉选择",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_tree_option(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="dept.options", tenant=tenant
        )
    )


@router.get(
    "/tree-list",
    response_model=ResponseModel[PageResult[ProjectedDeptOut]],
    summary="获取部门树形列表(带伪分页)",
    description="获取部门树形结构，包装为 PageResult 适配前端",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_tree_list(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="dept.tree-list", tenant=tenant
        )
    )


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[ProjectedDeptOut]],
    summary="获取部门分页列表",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_list(
    query: DeptQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="dept.list", tenant=tenant, query=query
        )
    )


@router.get(
    "/{dept_id}",
    response_model=ResponseModel[ProjectedDeptOut],
    summary="获取部门详情",
    dependencies=[Depends(require_permissions("system:dept:list"))],
)
async def get_dept_detail(
    dept_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="dept.detail", tenant=tenant, target_id=dept_id
        )
    )


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
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """创建部门"""
    await system_write(
        db, current_user, operation="dept.create", payload=dept_in, tenant=tenant
    )
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
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """更新部门"""
    await system_write(
        db,
        current_user,
        operation="dept.update",
        payload=dept_in,
        target_id=dept_id,
        tenant=tenant,
    )
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
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """Move one department subtree through the shared write policy."""
    await system_write(
        db,
        current_user,
        operation="dept.move",
        payload=move_in,
        target_id=dept_id,
        tenant=tenant,
    )
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
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """删除部门"""
    await system_write(
        db, current_user, operation="dept.delete", target_id=dept_id, tenant=tenant
    )
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
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """批量删除部门"""
    outcome = await system_write(
        db,
        current_user,
        operation="dept.batch_delete",
        payload=ids,
        tenant=tenant,
    )
    deleted_count = outcome["result"]
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 个部门")


@router.get(
    "/{dept_id}/users",
    response_model=ResponseModel[PageResult[ProjectedUserOut]],
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
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """Return one user-scoped page of membership candidates."""
    page = await user_read(
        db,
        current_user,
        operation="members",
        tenant=tenant,
        query=UserMemberQuery(dept_id=dept_id, query=query, current=current, size=size),
    )
    return ResponseModel.success(data=page)


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
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """Replace the complete department member set atomically."""
    outcome = await system_write(
        db,
        current_user,
        operation="dept.members",
        payload=body.user_ids,
        target_id=dept_id,
        tenant=tenant,
    )
    result = outcome["result"]
    return ResponseModel.success(
        msg=f"新增 {result.added} 人，移除 {result.removed} 人"
    )
