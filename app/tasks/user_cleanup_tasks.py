"""用户导入导出定时清理任务。

3 个 ``@register_task`` 入口，对应 ``sys_job.job_key``：
- ``clean_expired_import_batches``：每日 02:00，删 90 天前终态 batch + 文件
- ``clean_expired_import_previews``：每小时，PREVIEW_DONE > 10min → EXPIRED
- ``clean_expired_export_tasks``：每日 02:30，删 30 天前 ExportTask + 文件

service 层函数（``cleanup_expired_*``）只接 ``db`` 参数不 commit，
本模块负责 ``AsyncSessionLocal()`` 上下文 + commit。
"""

import logging
from uuid import uuid4

from app.core.tenant import PlatformContext
from app.db.session import AsyncSessionLocal
from app.modules.job.task_registry import TaskScope, register_task
from app.modules.system.service.user_export_service import cleanup_expired_export_tasks
from app.modules.system.service.user_import_service import (
    cleanup_expired_batches,
    cleanup_expired_previews,
)

logger = logging.getLogger(__name__)


def _retention_context(reason: str) -> PlatformContext:
    return PlatformContext(
        actor_user_id=0,
        reason=reason,
        correlation_id=uuid4().hex,
    )


@register_task("clean_expired_import_batches", scope=TaskScope.PLATFORM)
async def clean_expired_import_batches(_args: dict | None = None) -> int:
    """清理 90 天前导入批次 + 关联文件（每日 02:00）。

    清理过期预检批次和对应临时文件。
    """
    async with AsyncSessionLocal() as db:
        count = await cleanup_expired_batches(
            db,
            platform=_retention_context("scheduled import retention"),
        )
        await db.commit()
    logger.info("clean_expired_import_batches: cleaned %d batches", count)
    return count


@register_task("clean_expired_import_previews", scope=TaskScope.PLATFORM)
async def clean_expired_import_previews(_args: dict | None = None) -> int:
    """PREVIEW_DONE > 10min → EXPIRED + 删孤儿 preview 文件（每小时）。

    清理已完成批次的过期失败行文件。
    """
    async with AsyncSessionLocal() as db:
        count = await cleanup_expired_previews(
            db,
            platform=_retention_context("scheduled preview expiration"),
        )
        await db.commit()
    logger.info("clean_expired_import_previews: expired %d previews", count)
    return count


@register_task("clean_expired_export_tasks", scope=TaskScope.PLATFORM)
async def clean_expired_export_tasks(_args: dict | None = None) -> int:
    """清理 30 天前导出任务 + 关联文件（每日 02:30）。

    清理过期导出任务和对应文件。
    """
    async with AsyncSessionLocal() as db:
        count = await cleanup_expired_export_tasks(
            db,
            platform=_retention_context("scheduled export retention"),
        )
        await db.commit()
    logger.info("clean_expired_export_tasks: cleaned %d tasks", count)
    return count
