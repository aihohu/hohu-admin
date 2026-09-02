"""Trusted tenant principals shared by HTTP and worker execution paths."""

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Protocol

from app.constants import STATUS_ENABLED
from app.core.exceptions import AuthenticationException, AuthorizationException

DEFAULT_TENANT_ID = 0
DEFAULT_TENANT_CODE = "default"
_TENANT_CODE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])$")


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Immutable tenant authority derived from authenticated server state."""

    tenant_id: int
    tenant_code: str
    actor_user_id: int
    tenant_version: int
    source: Literal["access_token", "worker_envelope", "platform_control"]

    def __post_init__(self) -> None:
        if (
            isinstance(self.tenant_id, bool)
            or not isinstance(self.tenant_id, int)
            or self.tenant_id < 0
        ):
            raise ValueError("tenant_id must be a non-negative integer")
        if not _TENANT_CODE_RE.fullmatch(self.tenant_code):
            raise ValueError("tenant_code must be a normalized 2-32 character code")
        if (
            isinstance(self.actor_user_id, bool)
            or not isinstance(self.actor_user_id, int)
            or self.actor_user_id <= 0
        ):
            raise ValueError("actor_user_id must be a positive integer")
        if self.tenant_version < 1:
            raise ValueError("tenant_version must be positive")
        if self.source not in {
            "access_token",
            "worker_envelope",
            "platform_control",
        }:
            raise ValueError("tenant source is invalid")


@dataclass(frozen=True, slots=True)
class TenantLocatorContext:
    """Database-verified locator used only for unauthenticated public reads."""

    tenant_id: int
    tenant_code: str
    tenant_version: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.tenant_id, bool)
            or not isinstance(self.tenant_id, int)
            or self.tenant_id < 0
        ):
            raise ValueError("tenant_id must be a non-negative integer")
        if not _TENANT_CODE_RE.fullmatch(self.tenant_code):
            raise ValueError("tenant_code must be a normalized 2-32 character code")
        if self.tenant_version < 1:
            raise ValueError("tenant_version must be positive")


@dataclass(frozen=True, slots=True)
class PlatformContext:
    """Separate authority type for the future platform control plane."""

    actor_user_id: int
    reason: str
    correlation_id: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.actor_user_id, bool)
            or not isinstance(self.actor_user_id, int)
            or self.actor_user_id < 0
        ):
            raise ValueError("platform actor_user_id must be a non-negative integer")
        if (
            not isinstance(self.reason, str)
            or not self.reason.strip()
            or len(self.reason) > 256
            or any(ord(char) < 32 for char in self.reason)
        ):
            raise ValueError("platform reason is required")
        if (
            not isinstance(self.correlation_id, str)
            or not self.correlation_id.strip()
            or len(self.correlation_id) > 128
            or any(ord(char) < 32 for char in self.correlation_id)
        ):
            raise ValueError("platform correlation_id is required")


@dataclass(frozen=True, slots=True)
class TenantWorkerEnvelope:
    """Signed immutable tenant facts frozen when a worker job is enqueued."""

    tenant_id: int
    tenant_code: str
    actor_user_id: int
    tenant_version: int
    job_id: str
    scope_hash: str
    signature: str


class LiveTenantRecord(Protocol):
    tenant_id: int
    tenant_code: str
    status: str
    row_version: int


def normalize_tenant_code(value: str | None) -> str | None:
    """Normalize a login locator without treating it as authenticated context."""
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized if _TENANT_CODE_RE.fullmatch(normalized) else None


def bind_tenant_context(principal: Any, tenant: TenantContext) -> None:
    """Attach server-built context to an ORM principal after DB verification."""
    if (
        tenant.source != "access_token"
        or getattr(principal, "tenant_id", None) != tenant.tenant_id
        or getattr(principal, "user_id", None) != tenant.actor_user_id
    ):
        raise AuthenticationException(
            "租户上下文无效", error_code="TENANT_CONTEXT_INVALID"
        )
    principal._tenant_context = tenant


def get_bound_tenant_context(principal: Any) -> TenantContext:
    """Return only a context previously bound by the authentication dependency."""
    if isinstance(principal, TenantContext):
        return principal
    tenant = getattr(principal, "_tenant_context", None)
    if not isinstance(tenant, TenantContext):
        raise AuthenticationException(
            "缺少可信租户上下文", error_code="TENANT_CONTEXT_REQUIRED"
        )
    return tenant


def resolve_tenant_id(principal: Any) -> int:
    """Resolve only an immutable context bound by authentication or a worker."""
    return get_bound_tenant_context(principal).tenant_id


def _envelope_payload(envelope: TenantWorkerEnvelope) -> bytes:
    values = asdict(replace(envelope, signature=""))
    return json.dumps(values, sort_keys=True, separators=(",", ":")).encode()


def _sign_envelope(envelope: TenantWorkerEnvelope, secret: str) -> str:
    return hmac.new(
        secret.encode(), _envelope_payload(envelope), hashlib.sha256
    ).hexdigest()


def create_worker_envelope(
    tenant: TenantContext,
    *,
    job_id: str,
    scope_hash: str,
    secret: str,
) -> TenantWorkerEnvelope:
    """Freeze and sign tenant authority for a future background execution."""
    if not job_id or not scope_hash:
        raise ValueError("job_id and scope_hash are required")
    if tenant.source == "platform_control":
        raise ValueError(
            "platform-derived tenant scope cannot become tenant worker authority"
        )
    unsigned = TenantWorkerEnvelope(
        tenant_id=tenant.tenant_id,
        tenant_code=tenant.tenant_code,
        actor_user_id=tenant.actor_user_id,
        tenant_version=tenant.tenant_version,
        job_id=job_id,
        scope_hash=scope_hash,
        signature="",
    )
    return replace(unsigned, signature=_sign_envelope(unsigned, secret))


def revalidate_worker_envelope(
    envelope: TenantWorkerEnvelope,
    *,
    live_tenant: LiveTenantRecord,
    secret: str,
) -> TenantContext:
    """Verify an envelope and compare it with a freshly loaded tenant row."""
    expected_signature = _sign_envelope(envelope, secret)
    if not hmac.compare_digest(envelope.signature, expected_signature):
        raise AuthenticationException(
            "租户上下文无效", error_code="TENANT_CONTEXT_INVALID"
        )

    persisted_facts = (
        live_tenant.tenant_id,
        live_tenant.tenant_code,
        live_tenant.row_version,
    )
    envelope_facts = (
        envelope.tenant_id,
        envelope.tenant_code,
        envelope.tenant_version,
    )
    if persisted_facts != envelope_facts:
        raise AuthenticationException(
            "租户上下文无效", error_code="TENANT_CONTEXT_INVALID"
        )
    if live_tenant.status != STATUS_ENABLED:
        raise AuthorizationException("租户已被禁用", error_code="TENANT_DISABLED")

    return TenantContext(
        tenant_id=envelope.tenant_id,
        tenant_code=envelope.tenant_code,
        actor_user_id=envelope.actor_user_id,
        tenant_version=envelope.tenant_version,
        source="worker_envelope",
    )
