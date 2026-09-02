"""只读工具结果的查询回放缓存。

readonly tool 成功后，Gateway 把查询条件写入 Redis hash，5min TTL；
前端 chip 跳转带 ai_query_id=<trace_id>，模块页反查本缓存回放筛选。

Redis 结构：
    key:    ai:query_cache:<trace_id>     (Hash)
    field:  <tool_name>                    如 "user.list" / "user.stats"
    value:  JSON {
        "module": "system/user",          模块页路由前缀
        "filters": {"status": "1"},       回放筛选条件（按 allowed_filters 白名单过滤后）
        "tool_name": "user.list",
        "user_id": 100,                   owner 校验用
        "created_at": "2026-07-02T14:32:15Z"
    }
    ttl:    300s

支持同 trace_id 多 tool 写入（每个 tool 占 hash 一个 field）。
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings
from app.core.tenant import TenantContext
from app.modules.ai.agents.gateway.result import ResultProjection
from app.modules.ai.service.result_projection_service import (
    DATA_SCOPE_RESOLVER_VERSION,
    result_projection_service,
)

AI_QUERY_CACHE_PREFIX = "ai:query_cache:v3"
AI_QUERY_CACHE_TTL_SEC = 300
AI_QUERY_CACHE_SCHEMA_VERSION = 3
AI_QUERY_CACHE_LATEST_FIELD = "__latest_tool__"

_SET_QUERY_CACHE_LUA = """
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('HSET', KEYS[1], ARGV[3], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""


@dataclass(frozen=True)
class QueryCacheEntry:
    """Redis hash field value 的反序列化形式"""

    module: str
    filters: dict[str, Any]
    tool_name: str
    user_id: int
    tenant_id: int
    agent_code: str
    tool_codes: list[str]
    subject_refs: list[dict[str, str]]
    subject_refs_hash: str
    data_scope_hash: str | None
    resolver_version: str
    projection_dependency_message_ids: list[str]
    schema_version: int
    created_at: str  # ISO 8601 UTC

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.__dict__, ensure_ascii=False, default=str).encode(
            "utf-8"
        )


async def set_query_cache(
    redis: Redis,
    *,
    trace_id: str,
    tool_name: str,
    module: str,
    filters: dict[str, Any],
    user_id: int,
    tenant: TenantContext,
    agent_code: str,
    projection: ResultProjection | None,
    data_scope_hash: str | None,
    projection_dependency_message_ids: list[int] | tuple[int, ...] = (),
    ttl_sec: int = AI_QUERY_CACHE_TTL_SEC,
) -> None:
    """HSET ai:query_cache:<trace_id> <tool_name> <json> + EXPIRE <ttl>

    每次写入都会重置整个 hash 的 TTL，以覆盖同一 trace_id 下的多个工具。
    """
    if projection is None:
        raise ValueError("query cache requires complete trusted projection metadata")
    lineage = result_projection_service.freeze_lineage(
        tenant=tenant,
        agent_code=agent_code,
        tool_codes=[tool_name],
        subject_refs=projection.subject_refs,
        data_scope_hash=data_scope_hash if projection.scope_bound else None,
        projection_dependency_message_ids=projection_dependency_message_ids,
    )
    entry = QueryCacheEntry(
        module=module,
        filters=filters,
        tool_name=tool_name,
        user_id=user_id,
        tenant_id=lineage.tenant_id,
        agent_code=lineage.agent_code,
        tool_codes=list(lineage.tool_codes),
        subject_refs=list(lineage.subject_refs),
        subject_refs_hash=lineage.subject_refs_hash,
        data_scope_hash=lineage.data_scope_hash,
        resolver_version=lineage.resolver_version,
        projection_dependency_message_ids=[
            str(value) for value in lineage.projection_dependency_message_ids
        ],
        schema_version=AI_QUERY_CACHE_SCHEMA_VERSION,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if tool_name == AI_QUERY_CACHE_LATEST_FIELD:
        raise ValueError("tool_name collides with query-cache metadata")
    key = _key(trace_id, tenant=tenant)
    await redis.eval(
        _SET_QUERY_CACHE_LUA,
        1,
        key,
        tool_name,
        entry.to_json_bytes(),
        AI_QUERY_CACHE_LATEST_FIELD,
        ttl_sec,
    )


async def get_query_cache(
    redis: Redis,
    trace_id: str,
    *,
    tenant: TenantContext,
    tool_name: str | None = None,
) -> QueryCacheEntry | None:
    """取最新写入（按 created_at 降序）或指定 tool_name 的 entry

    读取规则：
      - 默认行为：取 hash 中最新写入（created_at 降序）
      - tool_name 给定：直接 HGET
      - hash 不存在 / field 不存在：返回 None
    """
    key = _key(trace_id, tenant=tenant)

    if tool_name is not None:
        body = await redis.hget(key, tool_name)
        if body is None:
            return None
        return _parse(body, tenant=tenant)

    # New writes publish the entry and latest pointer in one Lua transaction.
    latest = await redis.hget(key, AI_QUERY_CACHE_LATEST_FIELD)
    if latest is not None:
        if isinstance(latest, bytes):
            latest = latest.decode("utf-8")
        body = await redis.hget(key, latest)
        return _parse(body, tenant=tenant) if body is not None else None

    # Rolling-upgrade fallback for v3 entries written before the latest marker.
    all_entries = await redis.hgetall(key)
    if not all_entries:
        return None

    parsed: list[QueryCacheEntry] = []
    for raw in all_entries.values():
        entry = _parse(raw, tenant=tenant)
        if entry is not None:
            parsed.append(entry)

    if not parsed:
        return None

    parsed.sort(key=lambda e: e.created_at, reverse=True)
    return parsed[0]


async def delete_query_cache(
    redis: Redis, trace_id: str, *, tenant: TenantContext
) -> None:
    """显式删除（spec 没要求，调试用）"""
    await redis.delete(_key(trace_id, tenant=tenant))


def _key(trace_id: str, *, tenant: TenantContext) -> str:
    return f"{AI_QUERY_CACHE_PREFIX}:tenant:{tenant.tenant_id}:{trace_id}"


def _parse(raw: Any, *, tenant: TenantContext) -> QueryCacheEntry | None:
    """Redis 返回的 bytes/str 解析为 QueryCacheEntry"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        entry = QueryCacheEntry(**data)
    except (TypeError, ValueError):
        return None
    if entry.schema_version != AI_QUERY_CACHE_SCHEMA_VERSION:
        return None
    if entry.tenant_id != tenant.tenant_id:
        return None
    if entry.resolver_version != DATA_SCOPE_RESOLVER_VERSION:
        return None
    lineage = result_projection_service.lineage_from_record(entry)
    if lineage is None or (
        result_projection_service.subject_refs_hash(lineage.subject_refs)
        != lineage.subject_refs_hash
    ):
        return None
    return entry


# 测试 / 调试用：暴露 settings.TTL 给 lint
_ = settings
