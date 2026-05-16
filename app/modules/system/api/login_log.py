from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.schemas.login_log import LoginLogOut, LoginLogQuery
from app.modules.system.service.login_log_service import login_log_service

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[LoginLogOut]],
    summary="获取登录日志列表",
    dependencies=[Depends(require_permissions("monitor:login-log:list"))],
)
async def get_list(
    query: LoginLogQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user=Depends(get_current_user),
):
    page_data = await login_log_service.get_list(db, query)
    return ResponseModel.success(data=page_data)


@router.delete(
    "/clean",
    summary="清理登录日志",
    dependencies=[Depends(require_permissions("monitor:login-log:clean"))],
)
async def clean(
    days: int = Query(90, ge=1, description="清理多少天前的日志"),
    db: AsyncSession = Depends(get_db),
    _current_user=Depends(get_current_user),
):
    count = await login_log_service.clean(db, days)
    await db.commit()
    return ResponseModel.success(msg=f"已清理 {count} 条日志")


@router.post(
    "/batch-delete",
    summary="批量删除登录日志",
    dependencies=[Depends(require_permissions("monitor:login-log:delete"))],
)
async def batch_delete(
    ids: list[str] = Body(...),
    db: AsyncSession = Depends(get_db),
    _current_user=Depends(get_current_user),
):
    count = await login_log_service.batch_delete(db, ids)
    await db.commit()
    return ResponseModel.success(msg=f"已删除 {count} 条日志")
