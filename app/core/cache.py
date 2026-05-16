"""
Async Redis-based caching utility.

Provides both decorator and function-call interfaces for read-through caching,
cache eviction, and manual cache operations. All operations gracefully degrade
when Redis is unavailable.

Usage:
    # Decorator — read-through cache
    @cacheable(key="user:{id}", ttl=300)
    async def get_user(self, db, id: int): ...

    # Decorator — evict on write (prefer API layer, after commit)
    @cache_evict(pattern="config:*")
    async def create(self, db, config_in): ...

    # Function call — manual eviction (e.g. after commit)
    await cache_delete(pattern="config:*")

    # Function call — manual read
    data = await cache_get("config:public")
"""

import functools
import hashlib
import inspect
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase

from app.core.redis import redis_client

logger = logging.getLogger(__name__)

# Global prefix for all cache keys to avoid collisions with other Redis data
_KEY_PREFIX = "cache:"


def _serialize(obj: Any) -> str:
    """Serialize an arbitrary object to a JSON string."""

    def _default(o: Any) -> Any:
        # SQLAlchemy ORM object → extract column values
        if isinstance(o, DeclarativeBase):
            return {c.key: getattr(o, c.key) for c in sa_inspect(o).mapper.column_attrs}
        # Pydantic model
        if hasattr(o, "model_dump"):
            return o.model_dump()
        # Common types
        if isinstance(o, datetime):
            return o.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, time):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, UUID):
            return str(o)
        if isinstance(o, bytes):
            return o.decode("utf-8", errors="replace")
        return str(o)

    return json.dumps(obj, default=_default, ensure_ascii=False)


def _deserialize(data: str) -> Any:
    """Deserialize a JSON string."""
    return json.loads(data)


class _DotAccess(dict):
    """Dict wrapper that resolves dot-notation keys for format_map (e.g. {obj.attr})."""

    def __missing__(self, key: str) -> Any:
        if "." not in key:
            raise KeyError(key)
        obj_name, *attrs = key.split(".")
        obj = self[obj_name]
        for attr in attrs:
            obj = getattr(obj, attr)
        return obj


def _resolve_key(key_template: str, func: Callable, args: tuple, kwargs: dict) -> str:
    """Resolve a key template by replacing {param} placeholders with actual argument values.

    Supports:
        key="user:{id}"              → cache:user:42
        key="config:{query.group}"   → cache:config:basic  (attribute access via dot notation)
    """
    sig = inspect.signature(func)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()

    ctx = dict(bound.arguments)
    ctx.pop("self", None)
    ctx.pop("db", None)
    ctx.pop("kwargs", None)

    return f"{_KEY_PREFIX}{key_template.format_map(_DotAccess(ctx))}"


def _auto_key(func: Callable, args: tuple, kwargs: dict) -> str:
    """Auto-generate a cache key: ClassName.method_name:args_hash"""
    sig = inspect.signature(func)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()

    ctx = dict(bound.arguments)
    ctx.pop("self", None)
    ctx.pop("db", None)

    args_repr = _serialize(ctx)
    args_hash = hashlib.md5(args_repr.encode()).hexdigest()[:8]

    return f"{_KEY_PREFIX}{func.__qualname__}:{args_hash}"


def cacheable(*, key: str | None = None, ttl: int = 300):
    """Read-through cache decorator.

    Returns cached data on hit; executes the method and caches the result on miss.
    None results are not cached to prevent cache penetration.

    Note: Returns JSON-deserialized data (dict/list/str/number).
    Only use with methods that return JSON-serializable data.

    Args:
        key: Cache key template. Supports {param} and {obj.attr} placeholders.
             When None, auto-generates from function name + arguments.
        ttl: Cache expiration time in seconds (default: 300).
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            cache_key = (
                _resolve_key(key, func, args, kwargs)
                if key
                else _auto_key(func, args, kwargs)
            )

            # Try reading from cache
            try:
                cached = await redis_client.get(cache_key)
                if cached is not None:
                    return _deserialize(cached)
            except Exception as e:
                logger.warning("Cache read failed: %s | %s", cache_key, e)

            # Execute the method
            result = await func(*args, **kwargs)

            # Write to cache (skip None to prevent cache penetration)
            if result is not None:
                try:
                    await redis_client.setex(cache_key, ttl, _serialize(result))
                except Exception as e:
                    logger.warning("Cache write failed: %s | %s", cache_key, e)

            return result

        return wrapper

    return decorator


def cache_evict(*, key: str | None = None, pattern: str | None = None):
    """Cache eviction decorator. Clears cache after method execution.

    ⚠️ Prefer using this at the API layer (after commit), not the Service layer.
    Service methods don't commit — if commit fails, cache is already cleared,
    causing data inconsistency. Alternatively, call cache_delete() manually
    after commit in the API layer.

    Args:
        key: Exact cache key to delete.
        pattern: Glob pattern for batch deletion (e.g. "config:*"). Uses SCAN iteration.
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            await cache_delete(key=key, pattern=pattern)
            return result

        return wrapper

    return decorator


async def cache_delete(*, key: str | None = None, pattern: str | None = None):
    """Manually clear cache entries.

    Args:
        key: Exact cache key to delete (without the "cache:" prefix).
        pattern: Glob pattern for batch deletion (e.g. "config:*"). Uses SCAN iteration.
    """
    try:
        if key:
            await redis_client.delete(f"{_KEY_PREFIX}{key}")
        if pattern:
            keys = [
                k async for k in redis_client.scan_iter(match=f"{_KEY_PREFIX}{pattern}")
            ]
            if keys:
                await redis_client.delete(*keys)
    except Exception as e:
        logger.warning("Cache delete failed: %s | %s", key or pattern, e)


async def cache_get(key: str) -> Any | None:
    """Manually read from cache.

    Args:
        key: Cache key (without the "cache:" prefix).

    Returns:
        Cached data, or None if not found or on error.
    """
    try:
        cached = await redis_client.get(f"{_KEY_PREFIX}{key}")
        if cached is not None:
            return _deserialize(cached)
    except Exception as e:
        logger.warning("Cache get failed: %s | %s", key, e)
    return None
