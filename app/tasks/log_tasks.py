import logging

from app.modules.job.task_registry import register_task

logger = logging.getLogger(__name__)


@register_task("test_task")
async def test_task(_args: dict | None = None):
    """测试定时任务"""
    logger.info("test_task executed")
