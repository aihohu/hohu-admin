from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import TenantContext
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.schemas.operation_log import OperationLogQuery
from app.utils.pagination import build_filters, paginate


class OperationLogService:
    """操作审计日志业务逻辑服务"""

    async def get_list(
        self, db: AsyncSession, query: OperationLogQuery, *, tenant: TenantContext
    ):
        """获取操作日志分页列表。"""
        field_mapping = {
            "module": ("module", "=="),
            "action": ("action", "=="),
            "username": ("username", "contains"),
            "status_code": ("status_code", "=="),
            "start_time": ("create_time", ">="),
            "end_time": ("create_time", "<="),
        }
        filters = build_filters(SysOperationLog, field_mapping, **query.model_dump())
        filters.insert(0, SysOperationLog.tenant_id == tenant.tenant_id)
        return await paginate(
            db=db,
            model=SysOperationLog,
            query_params=query,
            filters=filters,
            order_by=SysOperationLog.create_time.desc(),
        )


operation_log_service = OperationLogService()
