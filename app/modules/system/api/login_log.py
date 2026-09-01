from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context
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
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    page_data = await login_log_service.get_list(db, query, tenant=tenant)
    return ResponseModel.success(data=page_data)
