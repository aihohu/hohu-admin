from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.core.tenant import TenantContext
from app.core.tenant_scope import tenant_filter
from app.modules.job.models.job import SysJobLog
from app.modules.job.schemas.job import JobLogQuery
from app.utils.pagination import build_filters, paginate


class JobLogService:
    """定时任务日志业务逻辑服务"""

    async def get_list(
        self, db: AsyncSession, query: JobLogQuery, *, tenant: TenantContext
    ):
        """获取任务日志分页列表。"""
        field_mapping = {
            "job_id": ("job_id", "=="),
            "job_key": ("job_key", "contains"),
            "status": ("status", "=="),
            "start_time": ("start_time", ">="),
            "end_time": ("start_time", "<="),
        }
        filters = build_filters(SysJobLog, field_mapping, **query.model_dump())
        filters.insert(0, tenant_filter(SysJobLog, tenant=tenant))
        return await paginate(
            db=db,
            model=SysJobLog,
            query_params=query,
            filters=filters,
            order_by=SysJobLog.start_time.desc(),
        )

    async def batch_delete(
        self, db: AsyncSession, ids: list[int], *, tenant: TenantContext
    ) -> int:
        """批量删除任务日志。"""
        normalized = set(ids)
        logs = list(
            (
                await db.execute(
                    select(SysJobLog).where(
                        SysJobLog.tenant_id == tenant.tenant_id,
                        SysJobLog.job_log_id.in_(normalized),
                    )
                )
            ).scalars()
        )
        if {int(log.job_log_id) for log in logs} != normalized:
            raise NotFoundException("任务日志")
        for log in logs:
            await db.delete(log)
        return len(logs)

    async def clean(self, db: AsyncSession, days: int, *, tenant: TenantContext) -> int:
        """清理指定天数前的日志。"""
        cutoff = datetime.now() - timedelta(days=days)
        stmt = delete(SysJobLog).where(
            SysJobLog.tenant_id == tenant.tenant_id,
            SysJobLog.start_time < cutoff,
        )
        result = await db.execute(stmt)
        return result.rowcount


job_log_service = JobLogService()
