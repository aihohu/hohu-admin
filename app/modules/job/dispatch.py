"""Closed short-lived notifications. Verification never authorizes execution."""

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.utils.closed_json import closed_object

_DOMAIN = b"hohu.job.dispatch/v1\x00"
_CONFIG_FIELDS = (
    "tenant_id",
    "job_id",
    "job_key",
    "cron_expression",
    "trigger_type",
    "interval_value",
    "interval_unit",
    "job_args",
    "status",
    "concurrent",
    "timeout_seconds",
    "max_retries",
    "run_on_enable",
)
_FIELDS = {
    "v",
    "dispatchId",
    "tenantId",
    "jobId",
    "actorId",
    "actorVersion",
    "gEpoch",
    "bEpoch",
    "jobDigest",
    "catalogDigest",
    "footprint",
    "issuedAt",
    "expiresAt",
}


def _error(code="JOB_DISPATCH_INVALID"):
    return BusinessException(
        409 if code == "JOB_DISPATCH_EXPIRED" else 403,
        "Job dispatch rejected",
        error_code=code,
    )


def _integer(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value < 2**63:
        raise _error()
    return value


def _id(value, *, zero=False):
    if (
        type(value) is not str
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 19
    ):
        raise _error()
    number = _integer(int(value), zero=zero)
    if str(number) != value:
        raise _error()
    return number


def _digest(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise _error()
    return value


def _now(value):
    return _integer(int(time.time()) if value is None else value, zero=True)


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value):
    try:
        result = base64.b64decode(
            value + "=" * ((-len(value)) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, UnicodeError):
        raise _error() from None
    if _b64(result) != value:
        raise _error()
    return result


def _mac(body):
    key = hashlib.sha256(_DOMAIN + settings.SECRET_KEY.encode("utf-8")).digest()
    return _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())


@dataclass(frozen=True, slots=True)
class DispatchClaims:
    dispatch_id: str
    tenant_id: int
    job_id: int
    actor_id: int
    actor_version: int
    g_epoch: int
    b_epoch: int
    job_digest: str
    catalog_digest: str
    issued_at: int
    expires_at: int


def _claims(data):
    if (
        set(data) != _FIELDS
        or type(data["v"]) is not int
        or data["v"] != 1
        or data["footprint"] != "job.run/v1"
    ):
        raise _error()
    identifier = data["dispatchId"]
    try:
        if (
            type(identifier) is not str
            or len(identifier) != 36
            or str(UUID(identifier)) != identifier
        ):
            raise _error()
    except ValueError:
        raise _error() from None
    issued, expires = _integer(data["issuedAt"], zero=True), _integer(data["expiresAt"])
    if not 0 < expires - issued <= 30:
        raise _error()
    return DispatchClaims(
        identifier,
        _id(data["tenantId"], zero=True),
        _id(data["jobId"]),
        _id(data["actorId"]),
        _integer(data["actorVersion"]),
        _integer(data["gEpoch"]),
        _integer(data["bEpoch"]),
        _digest(data["jobDigest"]),
        _digest(data["catalogDigest"]),
        issued,
        expires,
    )


def issue_dispatch(
    *,
    tenant_id,
    job_id,
    actor_id,
    actor_version,
    g_epoch,
    b_epoch,
    job_digest,
    catalog_digest,
    now=None,
):
    issued = _now(now)
    data = {
        "v": 1,
        "dispatchId": str(uuid4()),
        "tenantId": str(_integer(tenant_id, zero=True)),
        "jobId": str(_integer(job_id)),
        "actorId": str(_integer(actor_id)),
        "actorVersion": actor_version,
        "gEpoch": g_epoch,
        "bEpoch": b_epoch,
        "jobDigest": job_digest,
        "catalogDigest": catalog_digest,
        "footprint": "job.run/v1",
        "issuedAt": issued,
        "expiresAt": issued + 30,
    }
    _claims(data)
    _, encoded = closed_object(data, max_bytes=1536)
    body = _b64(encoded)
    token = body + "." + _mac(body)
    if len(token) > 2048:
        raise _error()
    return token


def verify_dispatch(token, *, now=None):
    if (
        type(token) is not str
        or not token.isascii()
        or not 1 <= len(token) <= 2048
        or token.count(".") != 1
    ):
        raise _error()
    body, signature = token.split(".")
    if not hmac.compare_digest(signature, _mac(body)):
        raise _error()
    try:
        data, canonical = closed_object(_decode(body).decode("utf-8"), max_bytes=1536)
        if _b64(canonical) != body:
            raise _error()
        claims = _claims(data)
    except (BusinessException, UnicodeError, ValueError, TypeError):
        raise _error() from None
    current = _now(now)
    if claims.issued_at > current + 2:
        raise _error()
    if current >= claims.expires_at:
        raise _error("JOB_DISPATCH_EXPIRED")
    return claims


def job_config_digest(job):
    if not isinstance(job, Mapping) or not set(_CONFIG_FIELDS) <= job.keys():
        raise _error()
    values = {key: job[key] for key in _CONFIG_FIELDS}
    arguments = values["job_args"]
    if arguments is not None and (
        type(arguments) is not str
        or len(arguments) > 8192
        or len(arguments.encode("utf-8")) > 8192
    ):
        raise _error()
    _, encoded = closed_object(values, max_bytes=16384)
    return hashlib.sha256(encoded).hexdigest()
