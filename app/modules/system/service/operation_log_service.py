from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.schemas.operation_log import OperationLogQuery
from app.utils.pagination import build_filters, paginate


class OperationLogService:
    """操作审计日志业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: OperationLogQuery):
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
        return await paginate(
            db=db,
            model=SysOperationLog,
            query_params=query,
            filters=filters,
            order_by=SysOperationLog.create_time.desc(),
        )

    async def batch_delete(self, db: AsyncSession, ids: list[str]) -> int:
        """批量删除操作日志。"""
        int_ids = [int(i) for i in ids]
        stmt = delete(SysOperationLog).where(
            SysOperationLog.operation_log_id.in_(int_ids)
        )
        result = await db.execute(stmt)
        return result.rowcount

    async def clean(self, db: AsyncSession, days: int) -> int:
        """清理指定天数前的操作日志。"""
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        stmt = delete(SysOperationLog).where(SysOperationLog.create_time < cutoff)
        result = await db.execute(stmt)
        return result.rowcount


operation_log_service = OperationLogService()
