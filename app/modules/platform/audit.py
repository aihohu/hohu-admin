"""Platform authorization envelope and independent append-only persistence."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException, BusinessException
from app.core.id_generator import next_id
from app.core.tenant import PlatformContext
from app.db.session import AsyncSessionLocal
from app.modules.platform.models import PlatformAuditLog


class PlatformPrincipalLike(Protocol):
    principal_id: int
    principal_name: str
    permissions: frozenset[str]


AuditPersist = Callable[..., Awaitable[int]]

_AUDIT_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"']?"
        r"[A-Za-z0-9_~+./=-]{8,}[\"']?"
    ),
)


@dataclass(frozen=True, slots=True)
class AuthorizedPlatformRequest:
    context: PlatformContext
    authorization_audit_id: int
    audit_path: str


def _clean_audit_value(
    value: str | None, *, max_length: int
) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, False
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > max_length
        or any(not character.isprintable() for character in cleaned)
    ):
        return None, False
    redacted = cleaned
    for pattern in _AUDIT_SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted, redacted != cleaned


def _canonicalize_ip(value: str | None) -> str | None:
    if not isinstance(value, str) or "%" in value:
        return None
    try:
        return ip_address(value.strip()).compressed
    except ValueError:
        return None


def _sanitize_request_summary(summary: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(summary, dict):
        return {}
    query_key_count = summary.get("queryKeyCount")
    if type(query_key_count) is int and 0 <= query_key_count <= 1000:
        return {"queryKeyCount": query_key_count}
    return {}


def _sanitize_result_summary(summary: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(summary, dict):
        return {}
    sanitized: dict[str, int] = {}
    status_code = summary.get("statusCode")
    if type(status_code) is int and 100 <= status_code <= 599:
        sanitized["statusCode"] = status_code
    for key in ("recordCount", "affectedCount"):
        value = summary.get(key)
        if type(value) is int and 0 <= value <= 2_147_483_647:
            sanitized[key] = value
    return sanitized


async def add_platform_audit(
    db: AsyncSession,
    *,
    actor_principal_id: int,
    actor_name: str,
    permission: str,
    event_type: str,
    method: str,
    path: str,
    reason: str | None,
    ticket_id: str | None,
    correlation_id: str | None,
    ip: str | None,
    request_summary: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
    target_tenant_id: int | None = None,
    authorization_audit_id: int | None = None,
    status_code: int | None = None,
    duration_ms: int | None = None,
    denial_code: str | None = None,
) -> int:
    """Add one audit event without committing the caller's transaction."""
    if event_type == "completed":
        raise BusinessException(
            code=400,
            message="平台完成审计必须使用幂等写入器",
            error_code="PLATFORM_AUDIT_COMPLETION_WRITER_REQUIRED",
        )
    safe_reason, _ = _clean_audit_value(reason, max_length=256)
    safe_ticket_id, _ = _clean_audit_value(ticket_id, max_length=128)
    safe_correlation_id, _ = _clean_audit_value(correlation_id, max_length=128)
    safe_path, _ = _clean_audit_value(path, max_length=256)
    event = PlatformAuditLog(
        actor_principal_id=actor_principal_id,
        actor_name=actor_name,
        permission=permission,
        event_type=event_type,
        method=method,
        path=safe_path or "[INVALID_PATH]",
        reason=safe_reason,
        ticket_id=safe_ticket_id,
        correlation_id=safe_correlation_id,
        ip=_canonicalize_ip(ip),
        request_summary=_sanitize_request_summary(request_summary) or None,
        result_summary=_sanitize_result_summary(result_summary) or None,
        target_tenant_id=target_tenant_id,
        authorization_audit_id=authorization_audit_id,
        status_code=status_code,
        duration_ms=duration_ms,
        denial_code=denial_code,
    )
    db.add(event)
    await db.flush()
    return event.audit_id


async def add_platform_completion(
    db: AsyncSession,
    *,
    authorization_audit_id: int,
    actor_principal_id: int,
    actor_name: str,
    permission: str,
    method: str,
    path: str,
    reason: str,
    ticket_id: str,
    correlation_id: str,
    ip: str | None,
    target_tenant_id: int | None,
    status_code: int,
    duration_ms: int,
    result_summary: dict[str, Any] | None,
) -> int:
    """Append one completion with database-atomic first-writer-wins replay."""
    safe_result_summary = _sanitize_result_summary(result_summary)
    if (
        not 100 <= status_code <= 599
        or duration_ms < 0
        or safe_result_summary.get("statusCode") != status_code
    ):
        raise BusinessException(
            code=400,
            message="平台完成审计字段无效",
            error_code="PLATFORM_AUDIT_COMPLETION_INVALID",
        )
    safe_reason, _ = _clean_audit_value(reason, max_length=256)
    safe_ticket_id, _ = _clean_audit_value(ticket_id, max_length=128)
    safe_correlation_id, _ = _clean_audit_value(correlation_id, max_length=128)
    safe_path, _ = _clean_audit_value(path, max_length=256)
    candidate_id = next_id()
    statement = (
        postgresql_insert(PlatformAuditLog)
        .values(
            audit_id=candidate_id,
            authorization_audit_id=authorization_audit_id,
            actor_principal_id=actor_principal_id,
            actor_name=actor_name,
            permission=permission,
            event_type="completed",
            method=method,
            path=safe_path or "[INVALID_PATH]",
            reason=safe_reason,
            ticket_id=safe_ticket_id,
            correlation_id=safe_correlation_id,
            ip=_canonicalize_ip(ip),
            target_tenant_id=target_tenant_id,
            status_code=status_code,
            duration_ms=duration_ms,
            result_summary=safe_result_summary,
        )
        .on_conflict_do_nothing(
            index_elements=[PlatformAuditLog.authorization_audit_id],
            index_where=PlatformAuditLog.authorization_audit_id.is_not(None),
        )
        .returning(PlatformAuditLog.audit_id)
    )
    inserted_id = await db.scalar(statement)
    if inserted_id is not None:
        return inserted_id

    existing = await db.scalar(
        select(PlatformAuditLog).where(
            PlatformAuditLog.authorization_audit_id == authorization_audit_id
        )
    )
    if existing is None:
        raise BusinessException(
            code=503,
            message="平台完成审计暂不可用",
            error_code="PLATFORM_AUDIT_UNAVAILABLE",
        )
    if existing.status_code != status_code:
        raise BusinessException(
            code=409,
            message="平台完成审计状态冲突",
            error_code="PLATFORM_AUDIT_COMPLETION_CONFLICT",
        )
    return existing.audit_id


async def persist_platform_audit(**values: Any) -> int:
    """Commit one event independently so business rollbacks cannot erase intent."""
    async with AsyncSessionLocal() as session:
        audit_id = await add_platform_audit(session, **values)
        await session.commit()
        return audit_id


async def persist_platform_completion(**values: Any) -> int:
    """Commit one idempotent completion independently from business state."""
    async with AsyncSessionLocal() as session:
        audit_id = await add_platform_completion(session, **values)
        await session.commit()
        return audit_id


async def _persist_or_fail(persist: AuditPersist, **values: Any) -> int:
    try:
        return await persist(**values)
    except BusinessException:
        raise
    except Exception as exc:
        raise BusinessException(
            code=503,
            message="平台审计暂不可用",
            error_code="PLATFORM_AUDIT_UNAVAILABLE",
        ) from exc


async def authorize_platform_request(
    *,
    principal: PlatformPrincipalLike,
    permission: str,
    method: str,
    path: str,
    reason: str | None,
    ticket_id: str | None,
    correlation_id: str | None,
    ip: str | None,
    request_summary: dict[str, Any] | None,
    target_tenant_id: int | None = None,
    persist: AuditPersist = persist_platform_audit,
) -> AuthorizedPlatformRequest:
    """Validate, audit, and freeze authority before business code can execute."""
    clean_reason, reason_sensitive = _clean_audit_value(reason, max_length=256)
    clean_ticket, ticket_sensitive = _clean_audit_value(ticket_id, max_length=128)
    clean_correlation, correlation_sensitive = _clean_audit_value(
        correlation_id, max_length=128
    )
    clean_path, path_sensitive = _clean_audit_value(path, max_length=256)
    safe_path = clean_path or "[INVALID_PATH]"
    common = {
        "actor_principal_id": principal.principal_id,
        "actor_name": principal.principal_name,
        "permission": permission,
        "method": method,
        "path": safe_path,
        "reason": clean_reason,
        "ticket_id": clean_ticket,
        "correlation_id": clean_correlation,
        "ip": _canonicalize_ip(ip),
        "request_summary": _sanitize_request_summary(request_summary),
        "target_tenant_id": target_tenant_id,
    }

    if reason_sensitive or ticket_sensitive or correlation_sensitive or path_sensitive:
        await _persist_or_fail(
            persist,
            **common,
            event_type="denied",
            status_code=400,
            denial_code="PLATFORM_AUDIT_CONTEXT_SENSITIVE",
        )
        raise BusinessException(
            code=400,
            message="平台审计上下文不能包含密码、Token 或密钥",
            error_code="PLATFORM_AUDIT_CONTEXT_SENSITIVE",
        )

    if not all((clean_reason, clean_ticket, clean_correlation)):
        await _persist_or_fail(
            persist,
            **common,
            event_type="denied",
            status_code=400,
            denial_code="PLATFORM_AUDIT_CONTEXT_REQUIRED",
        )
        raise BusinessException(
            code=400,
            message="平台操作必须提供原因、工单号和关联 ID",
            error_code="PLATFORM_AUDIT_CONTEXT_REQUIRED",
        )

    if permission not in principal.permissions:
        await _persist_or_fail(
            persist,
            **common,
            event_type="denied",
            status_code=403,
            denial_code="PLATFORM_PERMISSION_DENIED",
        )
        raise AuthorizationException(
            "平台权限不足", error_code="PLATFORM_PERMISSION_DENIED"
        )

    audit_id = await _persist_or_fail(
        persist,
        **common,
        event_type="authorized",
    )
    context = PlatformContext(
        actor_principal_id=principal.principal_id,
        actor_name=principal.principal_name,
        principal_type="human",
        permissions=principal.permissions,
        reason=clean_reason,
        ticket_id=clean_ticket,
        correlation_id=clean_correlation,
        target_tenant_id=target_tenant_id,
    )
    return AuthorizedPlatformRequest(
        context=context,
        authorization_audit_id=audit_id,
        audit_path=safe_path,
    )
