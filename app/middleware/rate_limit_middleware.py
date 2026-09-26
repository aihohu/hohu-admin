"""Shared, bounded request throttling without buffering streaming responses."""

import hashlib
import logging

from jose import JWTError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core import redis as redis_module
from app.core.cache import cacheable
from app.core.security import decode_access_token, decode_platform_access_token
from app.core.tenant import DEFAULT_TENANT_ID
from app.db.session import AsyncSessionLocal
from app.modules.system.service.settings_service import settings_service
from app.utils.ip_util import get_client_ip

logger = logging.getLogger(__name__)
_RATE_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return count <= tonumber(ARGV[1]) and 1 or 0
"""


async def consume_rate_limit(client, key: str, limit: int, window: int) -> bool:
    return bool(await client.eval(_RATE_LUA, 1, key, limit, window))


@cacheable(key="tenant:0:setting:request_limits", ttl=30)
async def request_limits() -> dict[str, int]:
    defaults = {"login": 5, "register": 3, "api": 100}
    try:
        async with AsyncSessionLocal() as db:
            values = await settings_service.values(db, tenant_id=DEFAULT_TENANT_ID)
        return {
            name: max(1, int(values[f"security:{name}_per_minute"]))
            for name in defaults
        }
    except (SQLAlchemyError, TypeError, ValueError):
        logger.warning("Request limit settings unavailable; using safe defaults")
        return defaults


def request_bucket(request: Request) -> tuple[str, str]:
    path = request.url.path.rstrip("/")
    ip = get_client_ip(request) or "unknown"
    if path in {"/auth/login", "/platform/auth/login"}:
        return "login", f"ip:{ip}"
    if path == "/auth/register":
        return "register", f"ip:{ip}"
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:]
        for decode, namespace in [
            (decode_access_token, "user"),
            (decode_platform_access_token, "platform"),
        ]:
            try:
                claims = decode(token)
                # This signature-verified identity selects only a throttle bucket;
                # normal authentication still verifies revocation and tenant status.
                return (
                    "api",
                    f"{namespace}:{claims.get('tid', 'platform')}:{claims['sub']}",
                )
            except (JWTError, ValueError, KeyError):
                continue
    return "api", f"ip:{ip}"


class RateLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") == "OPTIONS"
            or scope.get("path") in {"/health", "/metrics"}
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        category, identity = request_bucket(request)
        limit = (await request_limits())[category]
        digest = hashlib.sha256(identity.encode()).hexdigest()
        try:
            allowed = await consume_rate_limit(
                redis_module.redis_client,
                f"request-limit:{category}:{digest}",
                limit,
                60,
            )
        except (RedisError, OSError):
            # Login protection must not silently disappear during a Redis outage.
            response = JSONResponse(
                status_code=503,
                content={
                    "code": 503,
                    "msg": "Access protection unavailable",
                    "data": None,
                    "errorCode": "RATE_LIMIT_UNAVAILABLE",
                },
            )
        else:
            if allowed:
                await self.app(scope, receive, send)
                return
            response = JSONResponse(
                status_code=429,
                headers={"Retry-After": "60"},
                content={
                    "code": 429,
                    "msg": "Too many requests",
                    "data": None,
                    "errorCode": "RATE_LIMIT_EXCEEDED",
                },
            )
        await response(scope, receive, send)
