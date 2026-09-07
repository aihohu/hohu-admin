"""Real Redis concurrency regressions for tenant-scoped AI runtime state."""

import asyncio
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from tenant_helpers import tenant_context

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway.quota import check_l1_rate_limit
from app.modules.ai.agents.gateway.result import ResultProjection
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.hitl.query_cache import (
    delete_query_cache,
    get_query_cache,
    set_query_cache,
)


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        yield client
    finally:
        await client.aclose()


async def test_same_business_keys_are_independent_across_tenants(
    redis_client, monkeypatch
) -> None:
    """A/B may concurrently reuse trace, user, and confirmation IDs without collision."""
    tenant_a = tenant_context(tenant_id=61_001)
    tenant_b = tenant_context(tenant_id=61_002)
    marker = uuid4().hex
    trace_id = f"plan6-shared-trace-{marker}"
    confirmation_id = f"plan6-shared-confirmation-{marker}"
    user_id = 77_001
    monkeypatch.setattr(settings, "AI_HITL_MODE", "redis_pubsub")

    async def write_query(tenant, tenant_label: str) -> None:
        await set_query_cache(
            redis_client,
            trace_id=trace_id,
            tool_name="user.list",
            module="system/user",
            filters={"tenantLabel": tenant_label},
            user_id=user_id,
            tenant=tenant,
            agent_code="user_mgmt",
            projection=ResultProjection(),
            data_scope_hash=None,
        )

    async def create_pending(tenant) -> None:
        await hitl_manager.create_pending(
            redis_client,
            confirmation_id=confirmation_id,
            user_id=user_id,
            tenant=tenant,
            conversation_id=88_001,
            tool_call_id="shared-call",
            trace_id=trace_id,
            tool_name="user.create",
            args={"marker": marker},
        )

    try:
        await asyncio.gather(
            write_query(tenant_a, "A"),
            write_query(tenant_b, "B"),
            create_pending(tenant_a),
            create_pending(tenant_b),
        )
        quota_a, quota_b = await asyncio.gather(
            check_l1_rate_limit(redis_client, user_id, tenant=tenant_a, limit=1),
            check_l1_rate_limit(redis_client, user_id, tenant=tenant_b, limit=1),
        )
        query_a, query_b = await asyncio.gather(
            get_query_cache(redis_client, trace_id, tenant=tenant_a),
            get_query_cache(redis_client, trace_id, tenant=tenant_b),
        )
        pending_a, pending_b = await asyncio.gather(
            hitl_manager.get_pending(redis_client, confirmation_id, tenant=tenant_a),
            hitl_manager.get_pending(redis_client, confirmation_id, tenant=tenant_b),
        )

        assert quota_a[0] == quota_b[0] == 1
        assert query_a is not None and query_a.filters == {"tenantLabel": "A"}
        assert query_b is not None and query_b.filters == {"tenantLabel": "B"}
        assert pending_a is not None and pending_a.tenant_id == tenant_a.tenant_id
        assert pending_b is not None and pending_b.tenant_id == tenant_b.tenant_id

        with pytest.raises(BusinessRuleException) as exc_info:
            await check_l1_rate_limit(redis_client, user_id, tenant=tenant_a, limit=1)
        assert exc_info.value.error_code == "AI_RATE_LIMIT_USER_WRITE"

        quota_b_second = await check_l1_rate_limit(
            redis_client, user_id, tenant=tenant_b, limit=2
        )
        assert quota_b_second[0] == 2
    finally:
        await asyncio.gather(
            delete_query_cache(redis_client, trace_id, tenant=tenant_a),
            delete_query_cache(redis_client, trace_id, tenant=tenant_b),
            hitl_manager.delete_pending(redis_client, confirmation_id, tenant=tenant_a),
            hitl_manager.delete_pending(redis_client, confirmation_id, tenant=tenant_b),
        )
        await redis_client.delete(
            f"ai:tenant:{tenant_a.tenant_id}:write:{user_id}",
            f"ai:tenant:{tenant_b.tenant_id}:write:{user_id}",
        )
