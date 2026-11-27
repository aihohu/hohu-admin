"""Bounds for cache protection; these functions never own business commits."""

import asyncio
import logging
from functools import wraps

from redis.exceptions import RedisError


def bounded_auxiliary(*, best_effort=False):
    def decorate(function):
        @wraps(function)
        async def run(*args, **kwargs):
            try:
                async with asyncio.timeout(2):
                    return await function(*args, **kwargs)
            except (RedisError, TimeoutError) as exc:
                if best_effort:
                    logging.getLogger(__name__).warning(
                        "Auxiliary cache update unavailable"
                    )
                    return None
                if isinstance(exc, TimeoutError):
                    raise RedisError("Auxiliary protection timed out") from None
                raise

        return run

    return decorate
