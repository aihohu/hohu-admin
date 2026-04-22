from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.job.models.job import SysJobLog
from app.modules.job.schemas.job import JobLogQuery
from app.utils.pagination import build_filters, paginate


class JobLogService:
    """定时任务日志业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: JobLogQuery):
        """获取任务日志分页列表。"""
        field_mapping = {
            "job_id": ("job_id", "=="),
            "job_key": ("job_key", "contains"),
            "status": ("status", "=="),
            "start_time": ("start_time", ">="),
            "end_time": ("start_time", "<="),
        }
        filters = build_filters(SysJobLog, field_mapping, **query.model_dump())
        return await paginate(
            db=db,
            model=SysJobLog,
            query_params=query,
            filters=filters,
            order_by=SysJobLog.start_time.desc(),
        )

    async def batch_delete(self, db: AsyncSession, ids: list[int]) -> int:
        """批量删除任务日志。"""
        count = 0
        for log_id in ids:
            log = await db.get(SysJobLog, log_id)
            if log:
                await db.delete(log)
                count += 1
        return count

    async def clean(self, db: AsyncSession, days: int) -> int:
        """清理指定天数前的日志。"""
        cutoff = datetime.now() - timedelta(days=days)
        stmt = delete(SysJobLog).where(SysJobLog.start_time < cutoff)
        result = await db.execute(stmt)
        return result.rowcount


job_log_service = JobLogService()
