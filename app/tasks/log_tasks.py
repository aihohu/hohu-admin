import logging

from app.modules.job.task_registry import register_task

logger = logging.getLogger(__name__)


@register_task("clean_logs")
async def clean_logs(args: dict | None = None):
    """清理过期日志"""
    _days = 30
    if args and "days" in args:
        _days = args["days"]
    logger.info("clean_logs: cleaning logs older than %s days", _days)
    # TODO: 实现实际的日志清理逻辑


@register_task("test_task")
async def test_task(_args: dict | None = None):
    """测试定时任务"""
    logger.info("test_task executed")
