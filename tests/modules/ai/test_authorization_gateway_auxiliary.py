"""Auxiliary protection cannot retain a database transaction across Redis IO."""

import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError
from sqlalchemy import event

from app.core.exceptions import BusinessException
from app.db.authorization_http import get_authorization_runtime
from app.db.session import engine
from app.modules.ai.agents.gateway import executor, quota
from app.modules.ai.agents.safety import auto_disable, ip_blacklist
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_preview_atomic_completion import preview_case as preview_case


async def test_ip_denial_redis_has_no_database_transaction(preview_case, monkeypatch):
    deps = preview_case.deps
    deps.client_ip = "2001:db8::1"
    active, observations = set(), []
    original = ip_blacklist.get_ai_config_int

    async def force_read(*args, **kwargs):
        return await original(*args, **kwargs, force_refresh=True)

    async def redis_io(*_args, **_kwargs):
        observations.append(len(active))
        return 1000000

    monkeypatch.setattr(ip_blacklist, "get_ai_config_int", force_read)
    monkeypatch.setattr(ip_blacklist, "_allowlist_cache", {})
    monkeypatch.setattr(
        executor,
        "redis_client",
        AsyncMock(incr=redis_io, expire=redis_io, set=redis_io),
    )

    def begin(conn):
        active.add(id(conn))

    def end(conn):
        active.discard(id(conn))

    event.listen(engine.sync_engine, "begin", begin)
    event.listen(engine.sync_engine, "rollback", end)
    event.listen(engine.sync_engine, "commit", end)
    try:
        await executor._record_perm_denied_for_ip(deps, "user.count")
    finally:
        event.remove(engine.sync_engine, "begin", begin)
        event.remove(engine.sync_engine, "rollback", end)
        event.remove(engine.sync_engine, "commit", end)
    assert len(observations) >= 2
    assert observations == [0] * len(observations)


async def test_quota_configuration_enters_global_guard_first(preview_case):
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        # This resolver is not patched by the preview fixture.
        result = await quota._resolve_l1_global_limit(preview_case.deps.tenant)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert isinstance(result, int)
    locks = [sql for sql in statements if "FOR SHARE" in sql or "FOR UPDATE" in sql]
    assert locks and "sys_user_field_policy_global_guard" in locks[0]


@pytest.mark.parametrize(
    "resolver", [auto_disable._resolve_threshold, auto_disable._resolve_duration]
)
async def test_injection_configuration_enters_global_guard_first(
    preview_case, resolver
):
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert isinstance(await resolver(preview_case.deps.tenant), int)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    locks = [sql for sql in statements if "FOR SHARE" in sql or "FOR UPDATE" in sql]
    assert locks and "sys_user_field_policy_global_guard" in locks[0]


async def test_auxiliary_settings_reject_stale_tenant_and_commit_nothing(preview_case):
    commits = []

    def committed(_conn):
        commits.append(True)

    event.listen(engine.sync_engine, "commit", committed)
    try:
        values = await get_authorization_runtime().auxiliary.options(
            tenant=preview_case.deps.tenant
        )
        assert len(values) == 11
        assert type(values["ai:auto_disable:perm_denied_per_hour"]) is int
        with pytest.raises(TypeError):
            values["ai:auto_disable:perm_denied_per_hour"] = 0
        with pytest.raises(BusinessException):
            await get_authorization_runtime().auxiliary.options(
                tenant=replace(
                    preview_case.deps.tenant,
                    tenant_version=preview_case.deps.tenant.tenant_version + 1,
                )
            )
    finally:
        event.remove(engine.sync_engine, "commit", committed)
    assert commits == []


async def test_quota_cache_timeout_fails_closed(preview_case):
    async def stalled(*_args, **_kwargs):
        await asyncio.sleep(10)

    redis = AsyncMock(eval=stalled)
    started = time.monotonic()
    with pytest.raises(RedisError):
        await quota.check_l1_rate_limit(
            redis,
            preview_case.deps.tenant.actor_user_id,
            tenant=preview_case.deps.tenant,
            limit=20,
        )
    assert time.monotonic() - started < 2.5


async def test_quota_rollback_timeout_does_not_replace_business_failure(preview_case):
    async def stalled(*_args, **_kwargs):
        await asyncio.sleep(10)

    redis = AsyncMock(eval=stalled)
    started = time.monotonic()
    await quota.decr_quota(
        redis,
        preview_case.deps.tenant.actor_user_id,
        tenant=preview_case.deps.tenant,
        l1_member="original-member",
    )
    assert time.monotonic() - started < 2.5
