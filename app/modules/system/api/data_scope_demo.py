"""数据权限演示业务表 API。

端点真应用 data_scope 过滤，让不同 scope 的用户看到不同数据子集。
权限码遵循 system:data-scope-demo:* 命名（CLAUDE.md 按钮权限约定）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.data_scope_demo import (
    DataScopeDemoCreate,
    DataScopeDemoOut,
    DataScopeDemoQuery,
    DataScopeDemoUpdate,
)
from app.modules.system.service.data_scope_demo_service import (
    data_scope_demo_service,
)

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[DataScopeDemoOut]],
    summary="获取数据权限演示数据列表",
    description="应用 data_scope 过滤，不同 scope 用户看到不同子集",
    dependencies=[Depends(require_permissions("system:data-scope-demo:list"))],
)
async def get_data_scope_demo_list(
    query: DataScopeDemoQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """演示数据列表（核心：数据权限过滤生效）"""
    page_data = await data_scope_demo_service.get_list(db, query, current_user)
    return ResponseModel.success(data=page_data)


@router.post(
    "/add",
    response_model=ResponseModel[DataScopeDemoOut],
    summary="创建演示数据",
    description="dept_id/create_by 从 current_user 注入，前端无法伪造",
    dependencies=[Depends(require_permissions("system:data-scope-demo:add"))],
)
async def add_data_scope_demo(
    data_in: DataScopeDemoCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建演示数据"""
    demo = await data_scope_demo_service.create(db, data_in, current_user)
    await db.commit()
    await db.refresh(demo)
    return ResponseModel.success(data=demo, msg="创建成功")


@router.put(
    "/{demo_id}",
    response_model=ResponseModel[DataScopeDemoOut],
    summary="更新演示数据",
    dependencies=[Depends(require_permissions("system:data-scope-demo:edit"))],
)
async def update_data_scope_demo(
    demo_id: int,
    data_in: DataScopeDemoUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """更新演示数据（不允许改 dept_id/create_by）"""
    demo = await data_scope_demo_service.update(db, demo_id, data_in)
    await db.commit()
    await db.refresh(demo)
    return ResponseModel.success(data=demo, msg="更新成功")


@router.delete(
    "/{demo_id}",
    response_model=ResponseModel,
    summary="删除演示数据",
    dependencies=[Depends(require_permissions("system:data-scope-demo:delete"))],
)
async def delete_data_scope_demo(
    demo_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """删除演示数据"""
    await data_scope_demo_service.delete(db, demo_id)
    await db.commit()
    return ResponseModel.success(msg="删除成功")
