"""Best-effort preview hints; callers invoke only after a known DB commit."""

import asyncio
import json

from app.core import redis as redis_module


async def cache_preview(tenant_id, token, batch_id):
    try:
        async with asyncio.timeout(2):
            await redis_module.redis_client.setex(
                f"tenant:{tenant_id}:user_import:preview:{token}",
                600,
                json.dumps({"batch_id": batch_id}),
            )
    except Exception:
        pass  # The committed batch remains the authoritative lookup.


async def retire_preview_hint(tenant_id, token):
    try:
        async with asyncio.timeout(2):
            await redis_module.redis_client.delete(
                f"tenant:{tenant_id}:user_import:preview:{token}"
            )
    except Exception:
        pass
