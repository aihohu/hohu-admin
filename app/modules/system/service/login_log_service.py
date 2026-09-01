from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import TenantContext
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.schemas.login_log import LoginLogQuery
from app.utils.pagination import build_filters, paginate


class LoginLogService:
    """登录日志业务逻辑服务"""

    async def get_list(
        self, db: AsyncSession, query: LoginLogQuery, *, tenant: TenantContext
    ):
        """获取登录日志分页列表。"""
        field_mapping = {
            "username": ("username", "contains"),
            "status": ("status", "=="),
            "ip": ("ip", "=="),
            "start_time": ("login_time", ">="),
            "end_time": ("login_time", "<="),
        }
        filters = build_filters(SysLoginLog, field_mapping, **query.model_dump())
        filters.insert(
            0,
            (SysLoginLog.tenant_id == tenant.tenant_id)
            & (SysLoginLog.audit_scope == "tenant"),
        )
        return await paginate(
            db=db,
            model=SysLoginLog,
            query_params=query,
            filters=filters,
            order_by=SysLoginLog.login_time.desc(),
        )


login_log_service = LoginLogService()
