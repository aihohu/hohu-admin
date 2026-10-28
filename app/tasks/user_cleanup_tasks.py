"""Bounded scheduled FILE retention with fresh authorization proofs per item.

Historical rows remain available for completion verification. Returned counts
refer to artifacts retired during this page, not deleted database rows.
"""

import asyncio
import logging

from app.core.write_admission import admitted
from app.db import authorization_http
from app.modules.job.task_registry import TaskScope, register_task

logger = logging.getLogger(__name__)
_cursors: dict[str, tuple[int, str] | None] = {}
_scan_locks = {name: asyncio.Lock() for name in ("import", "preview", "export")}


async def _advance(name):
    async with _scan_locks[name]:
        page = (
            await authorization_http.get_authorization_runtime().transfer.cleanup_page(
                kind="export" if name == "export" else "import",
                category="preview" if name == "preview" else "terminal",
                after=_cursors.get(name),
                limit=256,
            )
        )
        _cursors[name] = page.next_cursor
        count = sum(status == "retired" for _, _, status in page.items)
        logger.info("Scheduled %s retention: retired %d artifacts", name, count)
        return count


@register_task("clean_expired_import_batches", scope=TaskScope.PLATFORM)
@admitted
async def clean_expired_import_batches(_args: dict | None = None) -> int:
    """Advance one page of terminal imports retained for at least 90 days."""
    return await _advance("import")


@register_task("clean_expired_import_previews", scope=TaskScope.PLATFORM)
@admitted
async def clean_expired_import_previews(_args: dict | None = None) -> int:
    """Expire ten-minute previews and independently prove their FILE retirement."""
    return await _advance("preview")


@register_task("clean_expired_export_tasks", scope=TaskScope.PLATFORM)
@admitted
async def clean_expired_export_tasks(_args: dict | None = None) -> int:
    """Advance one page of terminal exports retained for at least 30 days."""
    return await _advance("export")
