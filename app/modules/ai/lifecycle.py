"""AI lifecycle terminal cleanup orchestration."""

import asyncio
import logging
from datetime import datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import PlatformContext, TenantContext
from app.core.write_admission import admitted, is_closed
from app.db.authorization_http import get_authorization_runtime, release_request_reads
from app.modules.ai.agents.hitl.constants import (
    AI_CONFIRM_REDIS_PREFIX,
)
from app.modules.ai.agents.hitl.manager import PendingPayload, hitl_manager
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    FILE_TOOLS,
)
from app.modules.system.models.tenant import Tenant

logger = logging.getLogger(__name__)


def _require_platform(platform: PlatformContext) -> None:
    if not isinstance(platform, PlatformContext):
        raise TypeError("platform context is required")


async def _tenant_context(
    db: AsyncSession,
    *,
    tenant_id: int,
    actor_user_id: int,
    platform: PlatformContext,
) -> TenantContext:
    """Compatibility locator only; it does not issue a JWT or a lock proof."""
    _require_platform(platform)
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise RuntimeError(f"tenant {tenant_id} no longer exists")
    return TenantContext(
        tenant_id=tenant.tenant_id,
        tenant_code=tenant.tenant_code,
        actor_user_id=actor_user_id,
        tenant_version=tenant.row_version,
        source="platform_control",
    )


_recovery_after: tuple[int, int] | None = None
_recovery_startup_before: datetime | None = None
_recovery_scan_lock = asyncio.Lock()


@admitted
async def cleanup_prepared_actions_on_startup(
    redis: Redis, *, platform: PlatformContext, expired_only: bool = False
) -> int:
    """Advance one bounded page; periodic calls finish the startup snapshot."""
    global _recovery_after, _recovery_startup_before
    _require_platform(platform)
    async with _recovery_scan_lock:
        owner = get_authorization_runtime().recovery
        if not expired_only and _recovery_startup_before is None:
            _recovery_startup_before = await owner.cutoff()
            _recovery_after = None
        page = await owner.sweep_page(
            after=_recovery_after,
            startup=_recovery_startup_before is not None,
            startup_before=_recovery_startup_before,
            redis=redis,
            repair_display=True,
        )
        _recovery_after = page.next_cursor
        if _recovery_after is None:
            _recovery_startup_before = None
        return sum(item.changed for item in page.items)


async def run_pending_expiry_loop(*, platform: PlatformContext) -> None:
    """Close offline approvals after their deadline without touching execution."""
    from app.core.redis import redis_client  # noqa: PLC0415

    _require_platform(platform)
    while not is_closed():
        await asyncio.sleep(30)
        try:
            await cleanup_prepared_actions_on_startup(
                redis_client, platform=platform, expired_only=True
            )
            if _legacy_cutoff is not None:
                await _cleanup_legacy_cache_page(redis_client, platform=platform)
        except Exception:
            logger.exception("pending confirmation expiry sweep failed; will retry")


async def finalize_orphaned_pending(
    db: AsyncSession,
    redis: Redis,
    *,
    confirmation_id: str,
    pending: PendingPayload,
    operation_log: AiOperationLog | None,
    tenant: TenantContext,
) -> None:
    """Compatibility handoff; the request session never owns a recovery write."""
    if (
        pending.action_id is not None
        or pending.tool_name in DB_TOOLS | FILE_TOOLS
        or (
            operation_log is not None
            and not getattr(operation_log, "readonly_snapshot", False)
        )
    ):
        return
    if pending.tenant_id != tenant.tenant_id or pending.user_id != tenant.actor_user_id:
        return
    await release_request_reads(db)
    owner = get_authorization_runtime().recovery
    await owner.orphan_cached(
        pending=pending,
        confirmation_id=confirmation_id,
        redis=redis,
        startup_before=await owner.cutoff(),
    )


_legacy_iterator = None
_legacy_cutoff: datetime | None = None
_legacy_scan_lock = asyncio.Lock()


@admitted
async def _cleanup_legacy_cache_page(redis, *, platform):
    global _legacy_iterator, _legacy_cutoff
    _require_platform(platform)
    async with _legacy_scan_lock:
        if _legacy_cutoff is None:
            return 0
        if _legacy_iterator is None:
            _legacy_iterator = redis.scan_iter(
                match=f"{AI_CONFIRM_REDIS_PREFIX}:tenant:*", count=64
            )
        owner = get_authorization_runtime().recovery
        cleaned = 0
        deadline = asyncio.get_running_loop().time() + 30
        for index in range(64):
            if index and asyncio.get_running_loop().time() >= deadline:
                break
            try:
                async with asyncio.timeout(2):
                    key = await anext(_legacy_iterator)
            except StopAsyncIteration:
                _legacy_iterator, _legacy_cutoff = None, None
                break
            except (RedisError, TimeoutError):
                _legacy_iterator = None
                logger.warning("Startup cache scan deferred")
                break
            try:
                key_text = key.decode() if isinstance(key, bytes) else str(key)
                prefix, tenant_value, confirmation_id = key_text.rsplit(":", 2)
                if (
                    prefix != f"{AI_CONFIRM_REDIS_PREFIX}:tenant"
                    or not tenant_value.isascii()
                    or not tenant_value.isdigit()
                    or len(tenant_value) > 19
                    or not 1 <= len(confirmation_id) <= 64
                ):
                    continue
                tenant_id = int(tenant_value)
                if tenant_id >= 2**63:
                    continue
                async with asyncio.timeout(2):
                    pending = await hitl_manager.get_pending_for_platform(
                        redis, confirmation_id, tenant_id=tenant_id, platform=platform
                    )
                if pending is None:
                    continue
                observed = await owner.observe_cached(
                    tenant_id=tenant_id,
                    user_id=pending.user_id,
                    confirmation_id=confirmation_id,
                    redis=redis,
                )
                if observed is not None:
                    cleaned += observed.disposition in {
                        "expired",
                        "failed",
                        "succeeded",
                        "rejected",
                    }
                elif pending.action_id is None:
                    cleaned += await owner.orphan_cached(
                        pending=pending,
                        confirmation_id=confirmation_id,
                        redis=redis,
                        startup_before=_legacy_cutoff,
                    )
            except Exception:
                logger.warning("Startup cache entry retained for later verification")
        return cleaned


@admitted
async def cleanup_orphaned_pending_on_startup(*, platform: PlatformContext) -> int:
    """Advance durable recovery and one bounded legacy cache page."""
    global _legacy_iterator, _legacy_cutoff
    from app.core.redis import redis_client  # noqa: PLC0415

    _require_platform(platform)
    cleaned = await cleanup_prepared_actions_on_startup(redis_client, platform=platform)
    async with _legacy_scan_lock:
        _legacy_cutoff = await get_authorization_runtime().recovery.cutoff()
        _legacy_iterator = None
    return cleaned + await _cleanup_legacy_cache_page(redis_client, platform=platform)


@admitted
async def cleanup_durable_prepared_actions_on_startup(
    *, platform: PlatformContext
) -> int:
    """Recover durable state in every HITL mode without scanning legacy cache."""
    from app.core.redis import redis_client  # noqa: PLC0415

    return await cleanup_prepared_actions_on_startup(redis_client, platform=platform)
