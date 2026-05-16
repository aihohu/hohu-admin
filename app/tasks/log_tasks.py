import logging

from app.db.session import AsyncSessionLocal
from app.modules.job.task_registry import register_task
from app.modules.system.service.login_log_service import login_log_service
from app.modules.system.service.operation_log_service import operation_log_service

logger = logging.getLogger(__name__)


@register_task("clean_operation_logs")
async def clean_operation_logs(args: dict | None = None):
    """清理过期操作日志"""
    days = 90
    if args and "days" in args:
        days = args["days"]
    logger.info("clean_operation_logs: cleaning logs older than %s days", days)
    async with AsyncSessionLocal() as db:
        count = await operation_log_service.clean(db, days)
        await db.commit()
        logger.info("clean_operation_logs: cleaned %d records", count)


@register_task("clean_login_logs")
async def clean_login_logs(args: dict | None = None):
    """清理过期登录日志"""
    days = 90
    if args and "days" in args:
        days = args["days"]
    logger.info("clean_login_logs: cleaning logs older than %s days", days)
    async with AsyncSessionLocal() as db:
        count = await login_log_service.clean(db, days)
        await db.commit()
        logger.info("clean_login_logs: cleaned %d records", count)


@register_task("test_task")
async def test_task(_args: dict | None = None):
    """测试定时任务"""
    logger.info("test_task executed")
