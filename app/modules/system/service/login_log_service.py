from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.schemas.login_log import LoginLogQuery
from app.utils.pagination import build_filters, paginate


class LoginLogService:
    """登录日志业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: LoginLogQuery):
        """获取登录日志分页列表。"""
        field_mapping = {
            "username": ("username", "contains"),
            "status": ("status", "=="),
            "ip": ("ip", "=="),
            "start_time": ("login_time", ">="),
            "end_time": ("login_time", "<="),
        }
        filters = build_filters(SysLoginLog, field_mapping, **query.model_dump())
        return await paginate(
            db=db,
            model=SysLoginLog,
            query_params=query,
            filters=filters,
            order_by=SysLoginLog.login_time.desc(),
        )

    async def batch_delete(self, db: AsyncSession, ids: list[str]) -> int:
        """批量删除登录日志。"""
        int_ids = [int(i) for i in ids]
        stmt = delete(SysLoginLog).where(SysLoginLog.login_log_id.in_(int_ids))
        result = await db.execute(stmt)
        return result.rowcount

    async def clean(self, db: AsyncSession, days: int) -> int:
        """清理指定天数前的登录日志。"""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = delete(SysLoginLog).where(SysLoginLog.login_time < cutoff)
        result = await db.execute(stmt)
        return result.rowcount


login_log_service = LoginLogService()
