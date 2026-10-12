"""Short-lived, tenant/purpose/identity-bound email challenges."""

import hashlib
import hmac
import secrets

from redis.exceptions import RedisError

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.core.redis import redis_client

_ISSUE = """
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SET', KEYS[2], '1', 'EX', 60)
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('DEL', KEYS[3])
return 1
"""
_VERIFY = """
local value = redis.call('GET', KEYS[1])
if not value then return 0 end
local tries = redis.call('INCR', KEYS[2])
if tries == 1 then redis.call('EXPIRE', KEYS[2], redis.call('TTL', KEYS[1])) end
local total = redis.call('INCR', KEYS[3])
if total == 1 then redis.call('EXPIRE', KEYS[3], 3600) end
if tries > 5 or total > 30 then redis.call('DEL', KEYS[1]); return 0 end
if value ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""
_LIMIT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


def digest(value: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


def verification_error(message, error_code, status):
    error = BusinessRuleException(message, error_code=error_code)
    error.code = status
    return error


class EmailVerification:
    def key(self, tenant_id, purpose, identity):
        return f"tenant:{tenant_id}:email-code:{purpose}:{digest(identity)}"

    async def throttle(self, tenant_id, email, ip):
        try:
            for bucket, limit in (
                ("email:" + digest(email.casefold()), 10),
                ("ip:" + digest(ip), 30),
                ("tenant", 500),
            ):
                key = f"tenant:{tenant_id}:email-rate:{bucket}"
                if await redis_client.eval(_LIMIT, 1, key, 3600) > limit:
                    raise verification_error(
                        "Too many email requests", "EMAIL_RATE_LIMITED", 429
                    )
        except RedisError:
            raise verification_error(
                "Verification is temporarily unavailable",
                "EMAIL_VERIFICATION_UNAVAILABLE",
                503,
            ) from None

    async def issue(self, tenant_id, purpose, identity, minutes):
        key = self.key(tenant_id, purpose, identity)
        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            # Reset this challenge's attempts; cumulative verification limits survive.
            allowed = await redis_client.eval(
                _ISSUE,
                3,
                key,
                key + ":cooldown",
                key + ":attempts",
                digest(key + code),
                minutes * 60,
            )
            if not allowed:
                raise verification_error(
                    "Wait 60 seconds before resending", "EMAIL_RATE_LIMITED", 429
                )
        except RedisError:
            raise verification_error(
                "Verification is temporarily unavailable",
                "EMAIL_VERIFICATION_UNAVAILABLE",
                503,
            ) from None
        return code

    async def consume(self, tenant_id, purpose, identity, code):
        key = self.key(tenant_id, purpose, identity)
        try:
            valid = await redis_client.eval(
                _VERIFY, 3, key, key + ":attempts", key + ":total", digest(key + code)
            )
        except RedisError:
            raise verification_error(
                "Verification is temporarily unavailable",
                "EMAIL_VERIFICATION_UNAVAILABLE",
                503,
            ) from None
        if not valid:
            raise BusinessRuleException(
                "Invalid or expired email code", error_code="EMAIL_CODE_INVALID"
            )


email_verification = EmailVerification()
